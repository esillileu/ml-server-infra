mod psql 'just/psql'
mod seaweed 'just/seaweed'

# Install svc-ln and its user unit without enabling it.
svc-ln-deploy:
    ./svc-ln/deploy
