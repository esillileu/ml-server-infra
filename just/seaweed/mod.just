set shell := ["bash", "-euo", "pipefail", "-c"]

root := justfile_directory()
tool := root + "/scripts/seaweedfs/seaweed"

bootstrap-buckets:
    {{tool}} bootstrap-buckets

verify-f1-known-good:
    {{tool}} verify-f1-known-good

status:
    {{tool}} status

smoke-f1:
    {{tool}} smoke-f1

smoke-f2:
    {{tool}} smoke-f2

test-s3-endpoint endpoint="https://esillileu-server.tail4941d3.ts.net:9000":
    {{tool}} test-s3-endpoint {{endpoint}}

smoke-multipart-f1:
    {{tool}} smoke-multipart-f1

smoke-multipart-f2:
    {{tool}} smoke-multipart-f2

rotate-corpus-key:
    {{root}}/scripts/seaweedfs/rotate-corpus-key.sh
