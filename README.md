This is a simple set of Python scripts to test
[IMP](https://integrativemodeling.org/) applications on a compute cluster,
and to deposit those test results into a database that can be seen at
https://integrativemodeling.org/systems/.

For the frontend for the website, see https://github.com/salilab/systems-web/.

## Configuration

Copy the file `config.yaml.in` to `config.yaml` and fill in the necessary
paths, machine names, and credentials.

## Usage

 1. To start tests of IMP applications, run `test.py` on the compute cluster
    headnode, giving as arguments the IMP branch and git hash to test with.
    This will create a directory with subdirectories for each application,
    and submit jobs to the cluster.
 2. To check on the jobs and collect test results, run `test.py` with no
    arguments in the directory that was created above. This will place test
    results in a Python pickle file.
 3. Run `test.py` with the path to the pickle file, on the machine running the
    MySQL database, to deposit test results into the database.
