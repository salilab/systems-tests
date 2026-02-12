from __future__ import print_function
import sys
import re
import os
import stat
import urllib.parse
import urllib.request
import glob
import datetime
import subprocess
import json
import hashlib
import functools
import yaml
import pathlib

def read_config():
    p = pathlib.Path(__file__)
    with open(p.parent / 'config.yaml') as fh:
        return yaml.safe_load(fh)


class Environment(object):
    pass

class _ModulesEnvironment(Environment):
    def __init__(self, imp):
        pass
    def setup_working_directory(self):
        pass
    def setup_system(self, system):
        pass

    def add_python_to_path(self, python):
        """Make sure that the given Python is the first 'python' in PATH"""
        return ["PYDIR=$(mktemp -d $(pwd)/python-env.XXXXXX)",
                "ln -sf /usr/bin/%s ${PYDIR}/python" % python,
                "export PATH=${PYDIR}:$PATH"]

    def get_python_setup_script(self, system, python):
        def module_load(m):
            pymajor = python.split(".")[0]
            return 'module load ' + m.replace('python/', pymajor + '/')
        lines = self.add_python_to_path(python)
        lines.append(system.imp.get_shell_init(system.build_mode))
        if system.use_modeller:
            lines.append(system.modeller.get_shell_init())
        # Some tests need BinaryCIF support, which needs msgpack
        lines.append(module_load("python/msgpack"))
        for m in system.modules:
            lines.append(module_load(m))
        if system.parallel:
            lines.append(module_load("mpi/openmpi-x86_64"))
        return "\n".join(lines)


class Py2ModulesEnvironment(_ModulesEnvironment):
    """Run all tests using system /usr/bin/python2 plus modules"""
    def get_system_setup_script(self, system):
        return self.get_python_setup_script(system, 'python2')

# Backwards compatibility
ModulesEnvironment = Py2ModulesEnvironment


class Py3ModulesEnvironment(_ModulesEnvironment):
    """Run all tests using system /usr/bin/python3 plus modules"""
    python_prefix = 'python3/'

    def get_system_setup_script(self, system):
        # The cluster has multiple Pythons, but 3.6 probably has the most
        # modules available (e.g. numpy)
        return self.get_python_setup_script(system, 'python3.6')


class CondaEnvironment(Environment):
    """Run all tests using Anaconda Python"""
    URL = 'https://repo.continuum.io/miniconda'
    VER = '4.5.4'
    SHA256 = '80ecc86f8c2f131c5170e43df489514f80e3971dd105c075935470bbf2476dea'
    PACKAGES = ['modeller', 'biopython', 'scikit-learn', 'numpy',
                'scipy', 'pyparsing']

    def __init__(self, imp, modeller):
        self.imp_package = {'master': 'imp',
                            'develop': 'imp-nightly'}[imp.branch]
        self.modeller = modeller

    def setup_working_directory(self):
        # Make sure that conda Modeller package can set the license key
        os.environ['KEY_MODELLER'] = self.modeller.license
        self.install_miniconda()

    def _get_sha256(self, fname, chunk_size=65536):
        digest = hashlib.sha256()
        with open(fname, 'rb') as f:
            for chunk in iter(functools.partial(f.read, chunk_size), ''):
                digest.update(chunk)
        return digest.hexdigest()

    def install_miniconda(self):
        """Install Miniconda in the current directory."""
        self.miniconda_top = os.path.join(os.getcwd(), 'miniconda3')
        self.conda_bin = os.path.join(self.miniconda_top, 'bin', 'conda')
        # get installer and check sha256
        installer = 'Miniconda3-%s-Linux-x86_64.sh' % self.VER
        subprocess.check_call(['wget', '%s/%s' % (self.URL, installer)])
        if self._get_sha256(installer) != self.SHA256:
            raise ValueError("conda installer sha256 mismatch")

        # run installer
        subprocess.check_call(['/bin/bash', installer, '-b', '-p',
                               self.miniconda_top])
        os.unlink(installer)

        # Install all packages. This allows environments
        # for each system to hardlink those packages rather than downloading
        # and reinstalling them.
        subprocess.check_call([self.conda_bin, 'install', '-y',
                               '-c', 'salilab', self.imp_package]
                              + self.PACKAGES)
        # Make sure everything is up to date
        subprocess.check_call([self.conda_bin, 'update', '-y', '--all'])

    def _get_environment_name(self, system):
        return 'env-%s' % system.name

    def setup_system(self, system):
        env = self._get_environment_name(system)
        # Remove old environment (ignore errors, since it may not exist)
        subprocess.call([self.conda_bin, 'remove', '-n', env, '-y', '--all'])
        # Create a conda environment containing all packages the system needs
        # argparse is only needed for Python 2.6, and our conda install
        # is Python 3
        prereqs = [p for p in system.repo.conda_prereqs if p != 'argparse']
        # Some tests need BinaryCIF support, which needs msgpack
        prereqs.append("msgpack-python")
        if self.imp_package != 'imp' and 'imp' in prereqs:
            prereqs.remove('imp')
            prereqs.append(self.imp_package)
        subprocess.check_call([self.conda_bin, 'create', '-n', env,
                               '-c', 'salilab', '-y'] + prereqs)

    def get_system_setup_script(self, system):
        # Activate the conda environment for this system
        env = self._get_environment_name(system)
        return 'source %s/bin/activate %s' % (self.miniconda_top, env)

class IMPBuildMode(object):
    pass

class FastBuild(IMPBuildMode):
    def get_exetype(self):
        return "fast8"
    def get_sql(self):
        return "fast"

class ReleaseBuild(IMPBuildMode):
    def get_exetype(self):
        return "release8"
    def get_sql(self):
        return "release"

class DebugBuild(IMPBuildMode):
    def get_exetype(self):
        return "debug8"
    def get_sql(self):
        return "debug"

class IMP(object):
    def __init__(self, imp_top, branch, githash):
        top = os.path.join(imp_top, branch)
        g = glob.glob("%s/*-%s" % (top, githash[:10]))
        if len(g) > 0:
            self.topdir = sorted(g)[-1]
        else:
            raise ValueError("No IMP build in %s branch with githash %s" \
                             % (branch, githash))
        self.branch = branch
        with open("%s/build/imp-gitrev" % self.topdir) as f:
            self.githash = f.read().rstrip('\r\n')
        if branch == 'develop':
            self.version = None
        else:
            with open("%s/build/imp-version" % self.topdir) as f:
                self.version = f.read().rstrip('\r\n')

    def get_date(self):
        m = re.search(r'/(\d{8})\-[a-f0-9]+$', self.topdir)
        if m:
            return datetime.date(int(m.group(1)[:4]), int(m.group(1)[4:6]),
                                 int(m.group(1)[6:]))

    def get_shell_init(self, build_mode):
        exetype = build_mode.get_exetype()
        libdir = self.topdir + "/lib/" + exetype
        bindir = self.topdir + "/bin/" + exetype
        # Note that we assume we're the first package to init, so we clobber
        # existing library paths
        return "export LD_LIBRARY_PATH=%s\n" \
               "export PYTHONPATH=%s\n" \
               "PATH=%s:$PATH\n" \
               "module load Sali\n" \
               "module load boost libtau opencv python3/ihm sali-libraries" \
               % (libdir, libdir, bindir)


class Modeller(object):
    def __init__(self, license):
        self.license = license
        # Store in instance (not class) so it gets pickled
        self.version = '10.8'

    def get_shell_init(self):
        return "module load modeller/%s" % self.version


class Repo(object):
    def __init__(self, url, conda_prereqs):
        self.url = url
        self.conda_prereqs = conda_prereqs
        self.dirname = os.path.basename(urllib.parse.urlparse(url).path)

    def checkout(self):
        if os.path.exists(self.dirname):
            subprocess.check_call(["git", "pull", "-q"], cwd=self.dirname)
        else:
            subprocess.check_call(["git", "clone", "--depth", "5",
                                   "%s.git" % self.url])
            subprocess.check_call(["git", "submodule", "init"],
                                  cwd=self.dirname)
            subprocess.check_call(["git", "submodule", "update"],
                                  cwd=self.dirname)
        p = subprocess.Popen(["git", "rev-parse", "HEAD"],
                             stdout=subprocess.PIPE, cwd=self.dirname,
                             universal_newlines=True)
        self.version = p.communicate()[0].rstrip('\r\n')

    def parse_runtime(self, runtime):
        if runtime.endswith('d'):
            return int(runtime[:-1]) * 24
        elif runtime.endswith('h'):
            return int(runtime[:-1])
        else:
            raise ValueError("Cannot parse runtime: %s" % runtime)

    def parse_memory(self, memory):
        if not memory:
            return None
        elif memory.endswith('M'):
            return int(memory[:-1])
        elif memory.endswith('G'):
            return int(memory[:-1]) * 1024
        else:
            raise ValueError("Cannot parse memory: %s" % memory)

    def parse_metadata(self):
        import yaml
        y = yaml.load(open(os.path.join(self.dirname, 'metadata',
                                        'metadata.yaml')))
        test = y['test']

        modules = y.get('prereqs', [])
        if 'modeller' in modules:
            use_modeller = True
            modules.remove('modeller')
        else:
            use_modeller = False
        build_modes = {'fast': FastBuild, 'release': ReleaseBuild,
                       'debug': DebugBuild}
        return {'run_hours': self.parse_runtime(test['runtime']),
                'run_memory_mb': self.parse_memory(test.get('memory', None)),
                'parallel': int(test.get('parallel', 0)),
                'build_mode': build_modes[test.get('build', 'release')],
                'modules': modules, 'use_modeller': use_modeller}


class TestResult(object):
    def __init__(self, full_testname, errfile, result, time):
        self.full_testname = full_testname
        self.errfile = errfile
        self.result = result
        self.time = time

    def read_stderr(self):
        self.stderr = open(self.errfile).read()[:8192]

    def format_time(self):
        t = self.time
        if t < 120:
            return "%d seconds" % t
        t /= 60.
        if t < 120:
            return "%d minutes" % t
        t /= 60.
        if t < 48:
            return "%d hours" % t
        t /= 24.
        return "%d days" % t


class System(object):
    def __init__(self, name, repo, run_hours, run_memory_mb, parallel,
                 subdir=None, use_modeller=False, build_mode=DebugBuild,
                 modules=[]):
        self.name = name
        self.repo = repo
        self.subdir = subdir
        self.run_hours = run_hours
        self.run_memory_mb = run_memory_mb
        self.parallel = parallel
        self.modules = modules
        if run_hours > 336:
            raise ValueError("run_hours too large; job will never run")
        self.use_modeller = use_modeller
        if not issubclass(build_mode, IMPBuildMode):
            raise TypeError("build_mode should be FastBuild, "
                            "ReleaseBuild, or DebugBuild")
        self.build_mode = build_mode()

    def get_build_url(self):
        """Get the URL where the files for this *specific* build can be found"""
        # Note: currently assumes everything comes out of the master branch
        # from a github repository
        if self.subdir is None \
           or not self.repo.url.startswith('https://github'):
            raise ValueError("Currently assume everything is at github")
        return os.path.join(self.repo.url, 'tree', self.repo.version,
                            self.subdir)

    def get_tests(self):
        testdir = os.path.join(self.repo.dirname, self.subdir, 'test')
        tests = glob.glob("%s/test*.py" % testdir)
        if len(tests) == 0:
            raise ValueError("No tests for %s" % self.name)
        return tests

    def start_test(self, env, full_testname, imp, modeller):
        self.imp, self.modeller = imp, modeller
        testdir, testname = os.path.split(full_testname)
        script = full_testname + '.sge.sh'
        # Tests are supposed to be executable, but if they're not, assume
        # they're Python scripts
        if os.stat(full_testname).st_mode & stat.S_IXUSR:
            run_test = "./" + testname
        else:
            run_test = "python " + testname
        with open(script, 'w') as fh:
            print("#!/bin/sh", file=fh)
            print("#$ -S /bin/sh", file=fh)
            print("#$ -l h_rt=%d:0:0" % self.run_hours, file=fh)
            if self.run_memory_mb:
                print("#$ -l mem_free=%dM" % self.run_memory_mb, file=fh)
            else:
                # Prevent jobs from running on the most ancient Opterons
                # (for one, Anaconda needs SSE3 and so won't work there)
                print("#$ -l mem_free=2500M", file=fh)
            if self.parallel:
                print("#$ -pe mpi %d" % self.parallel, file=fh)
            print("#$ -N %s_%s"
                  % (re.sub(r'^([\d])', 'j\\1', self.name), testname), file=fh)
            print("#$ -cwd", file=fh)
            print("#$ -j y", file=fh)
            print("#$ -r y", file=fh)
            print("hostname", file=fh)
            errfile = testname + '.stderr'
            resfile = testname + '.result'
            timefile = testname + '.time'
            print("rm -f %s %s %s" % (errfile, resfile, timefile), file=fh)
            print(env.get_system_setup_script(self), file=fh)
            print("starttime=`date '+%s'`", file=fh)
            print("%s > /dev/null 2> %s" % (run_test, errfile), file=fh)
            print("echo $? > %s" % resfile, file=fh)
            print("endtime=`date '+%s'`", file=fh)
            print("echo $(( $endtime - $starttime )) > %s" % timefile, file=fh)
        subprocess.check_call(['qsub', os.path.basename(script)], cwd=testdir)

    def get_test_result(self, full_testname):
        errfile = full_testname + '.stderr'
        resfile = full_testname + '.result'
        timefile = full_testname + '.time'
        try:
            result = int(open(resfile).read().rstrip('\r\n'))
            time = int(open(timefile).read().rstrip('\r\n'))
            return TestResult(full_testname, errfile, result, time)
        except IOError:
            return


class Tester(object):
    def __init__(self, env, repos, systems, imp, modeller):
        self.env = env
        self.repos = repos
        self.systems = systems
        self.imp = imp
        self.modeller = modeller

    def start_tests(self):
        for s in self.systems:
            self.env.setup_system(s)
            tests = s.get_tests()
            for t in tests:
                s.start_test(self.env, t, self.imp, self.modeller)

    def collect_tests(self):
        system_results = []
        still_running = False
        running = done = failed = 0
        for s in self.systems:
            print()
            print(s.name)
            tests = s.get_tests()
            results = []
            for t in tests:
                result = s.get_test_result(t)
                results.append(result)
                if result is None:
                    restext = '(still running)'
                    running += 1
                    still_running = True
                else:
                    result.read_stderr()
                    if result.result == 0:
                        restext = 'completed successfully in %s' \
                                  % result.format_time()
                        done += 1
                    else:
                        restext = 'FAILED with code %s (ran in %s)' \
                                  % (result.result, result.format_time())
                        failed += 1
                print("    %-20s %s" % (os.path.basename(t)[:20], restext))
            system_results.append((s, results))
        print("\n%d completed, %d still running, %d failed"
              % (done, running, failed))
        return still_running, system_results

def get_all_repos():
    """Get a dict of all repos to test"""
    r = urllib.request.urlopen(
        'https://integrativemodeling.org/systems/api/list')
    contents = json.load(r)
    repos = {}
    for repo in contents:
        repos[repo['name']] = Repo(repo['repo'], repo['conda_prereqs'])
    return repos
