# Committed faults

Each file here describes faults that **must** turn a named test red. They are
run by `python3 tools/check.py --faults` and by CI, so a guard that quietly
stops guarding fails the build rather than waiting to be noticed.

A fault run by hand proves something once, on the day it was run. Every guard
in this project has a test, and a test that has never failed is a claim nobody
has checked — these are how the claim keeps being checked.

    python3 tools/faultcheck.py tools/faults/indoor.json

Adding one: name it as the mistake somebody would actually make, point it at
the code rather than the test, and make sure it changes behaviour. The runner
refuses a fault whose edit leaves the syntax tree identical, and reports one on
a line the suite never executes as UNRUN rather than as a missing test.
