set shell := ["bash", "-euo", "pipefail", "-c"]

root := justfile_directory()
tool := root + "/scripts/seaweedfs/seaweed"

preflight:
    {{tool}} preflight

bootstrap-buckets:
    {{tool}} bootstrap-buckets

copy-f1-completed:
    ionice -c3 nice -n 19 {{tool}} copy-f1-completed

verify-f1-completed:
    ionice -c3 nice -n 19 {{tool}} verify-f1-completed

copy-f1-final:
    ionice -c3 nice -n 19 {{tool}} copy-f1-final

verify-f1-final:
    ionice -c3 nice -n 19 {{tool}} verify-f1-final

verify-f1-existing:
    {{tool}} verify-f1-existing

status:
    {{tool}} status

smoke-f1:
    {{tool}} smoke-f1

smoke-f2:
    {{tool}} smoke-f2
