#!/usr/bin/python3

# We need Python 3.6 on the cluster as the default Python 3 (3.11) does not
# have the yaml module available

import sys
import os
import subprocess
from utils import IMP, Modeller, System, Tester, read_config, get_all_repos
from utils import Py3ModulesEnvironment, CondaEnvironment
import pickle


def parse_args():
    if len(sys.argv) == 3:
        return sys.argv[1], sys.argv[2], 'python3'
    elif len(sys.argv) == 4 and sys.argv[1] == '--conda':
        return sys.argv[2], sys.argv[3], sys.argv[1][2:]
    elif len(sys.argv) == 2:
        return sys.argv[1]
    elif len(sys.argv) != 1 or not os.path.exists('tester.pck'):
        print("Usage:", file=sys.stderr)
        print("To start a test run:", file=sys.stderr)
        print("    %s [--conda] branch githash"
              % sys.argv[0], file=sys.stderr)
        print("To collect results, run with no arguments", file=sys.stderr)
        print("in the test job working directory.", file=sys.stderr)
        sys.exit(1)


def make_working_dir(config, imp, envtype):
    home_prefix = config['cluster']['home_prefix']
    homedir = os.environ['HOME']
    if not homedir.startswith(home_prefix):
        raise ValueError("Home directory should be under %s" % home_prefix)
    workdir = os.path.join(homedir, 'imp_biosys_%s_%s_%s'
                           % (imp.branch.replace('/', '_'), imp.githash[:10],
                              envtype))
    if os.path.exists(workdir):
        os.chdir(workdir)
        return workdir
    else:
        os.mkdir(workdir)
        os.chdir(workdir)
        print("Working directory %s created" % workdir)
        return workdir


def assert_qsub(config):
    """Fail if qsub is not what it should be"""
    expected_qsub = config['cluster']['qsub']
    cluster_name = config['cluster']['name']
    qsub = subprocess.check_output(['which', 'qsub'],
                                   universal_newlines=True).rstrip('\r\n')
    if qsub != expected_qsub:
        raise ValueError("qsub is not %s-qsub" % cluster_name)


def start_tests(config, branch, githash, envtype):
    assert_qsub(config)
    # All repos that aren't working yet; don't bother to run tests on them
    not_working_yet = ["multifoxs_benchmark", "sampcon", "pemap", "npc_3.0",
                       "a3g-crl5"]
    repos = get_all_repos()
    for r in not_working_yet:
        del repos[r]
    imp = IMP(config['software']['imp_top'], branch, githash)
    modeller = Modeller(config['software']['modeller_license'])
    workdir = make_working_dir(config, imp, envtype)
    env = {'python3': Py3ModulesEnvironment,
           'conda': CondaEnvironment}[envtype](imp, modeller)
    env.setup_working_directory()
    for r in repos.values():
        r.checkout()
    systems = [System(name, repos[name], subdir='', **repo.parse_metadata())
               for name, repo in repos.items()]
    t = Tester(env, repos, systems, imp, modeller)
    t.start_tests()
    pickle.dump(t, open('tester.pck', 'wb'), -1)
    print()
    print("All test jobs started. Run this script again with no arguments,")
    print("in the %s directory," % workdir)
    print("to collect results.")


def collect_test_results(config):
    t = pickle.load(open('tester.pck', 'rb'))
    still_running, system_results = t.collect_tests()
    if still_running:
        print()
        print("At least one test is still running, so results cannot\n"
              "be collected yet. Please run this script again later.")
        print()
    else:
        pickle.dump((t, system_results), open('test_results.pck', 'wb'), -1)
        print()
        print("Test results collected in test_results.pck. Please copy this")
        print("file to %s, and run this script there on the file."
              % config['sql']['host'])
        print()


class SQLImporter:
    def __init__(self, fname):
        self.tester, self.system_results = pickle.load(open(fname, 'rb'))

    def connect_sql(self, config):
        import MySQLdb
        sql = config['sql']
        self.conn = MySQLdb.connect(passwd=sql['passwd'], db=sql['db'],
                                    user=sql['user'])

    def update_build(self, cur, imp_date, imp_githash, imp_version, imp_branch,
                     modeller_version):
        cur.execute("INSERT INTO sys_build (imp_date, imp_githash, "
                    "imp_version, imp_branch, modeller_version) "
                    "VALUES(%s,%s,%s,%s,%s)",
                    (imp_date, imp_githash, imp_version, imp_branch,
                     modeller_version))
        cur.execute("SELECT LAST_INSERT_ID()")
        return cur.fetchone()[0]

    def update_system_name(self, cur, name):
        cur.execute("SELECT id FROM sys_name WHERE name=%s", (name,))
        r = cur.fetchone()
        if r is not None:
            return r[0]
        else:
            cur.execute("INSERT INTO sys_name (name) VALUES(%s)", (name,))
            cur.execute("SELECT LAST_INSERT_ID()")
            return cur.fetchone()[0]

    def update_test_name(self, cur, system_id, name):
        cur.execute("SELECT id FROM sys_test_name WHERE sys=%s and name=%s",
                    (system_id, name))
        r = cur.fetchone()
        if r is not None:
            return r[0]
        else:
            cur.execute("INSERT INTO sys_test_name (sys, name) VALUES(%s,%s)",
                        (system_id, name))
            cur.execute("SELECT LAST_INSERT_ID()")
            return cur.fetchone()[0]

    def update_system_info(self, cur, build_id, system_id, system):
        cur.execute("INSERT INTO sys_info (build, sys, url, use_modeller, "
                    "imp_build_type) VALUES(%s, %s, %s, %s, %s)",
                    (build_id, system_id, system.get_build_url(),
                     system.use_modeller, system.build_mode.get_sql()))

    def update_system_results(self, cur, build_id, system_id, system, results):
        for r in results:
            name = os.path.basename(r.full_testname)
            name_id = self.update_test_name(cur, system_id, name)
            if r.result == 0:
                stderr = None
            else:
                stderr = r.stderr
            cur.execute("INSERT INTO sys_test (build, sys, name, retcode, "
                        "stderr, runtime) VALUES (%s, %s, %s, %s, %s, %s)",
                        (build_id, system_id, name_id, r.result, stderr,
                         r.time))

    def import_sql(self, config):
        self.connect_sql(config)
        cur = self.conn.cursor()
        build_id = self.update_build(cur, self.tester.imp.get_date(),
                                     self.tester.imp.githash,
                                     self.tester.imp.version,
                                     self.tester.imp.branch,
                                     self.tester.modeller.version)
        for system, results in self.system_results:
            system_id = self.update_system_name(cur, system.name)
            self.update_system_info(cur, build_id, system_id, system)
            self.update_system_results(cur, build_id, system_id, system,
                                       results)


def main():
    args = parse_args()
    config = read_config()
    if args is None:
        collect_test_results(config)
    elif isinstance(args, str):
        s = SQLImporter(args)
        s.import_sql(config)
    else:
        start_tests(config, *args)


if __name__ == '__main__':
    main()
