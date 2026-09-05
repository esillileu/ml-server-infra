create name:
    podman exec -it postgres \
        psql -U postgres -d postgres \
        -v name={{name}} \
        -f /opt/postgres/create-project.sql

drop name:
    podman exec -it postgres \
        psql -U postgres -d postgres \
        -v name={{name}} \
        -f /opt/postgres/drop-project.sql

change-pass name:
    /opt/infra/scripts/project/rotate-project-password.sh {{name}}