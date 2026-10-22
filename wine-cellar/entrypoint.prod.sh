#!/bin/sh
set -e

if [ "$DATABASE" = "postgres" ]
then
    echo "Waiting for postgres..."

    while ! nc -z $SQL_HOST $SQL_PORT; do
      sleep 0.1
    done

    echo "PostgreSQL started"
fi

if [ "$CONTAINER_ROLE" = "web" ]
then

# gunicorn will not migrate on a code reload — only a container start will.
# Only web applies schema. Celery/beat wait on --check so a compose restart
# (which ignores depends_on) cannot run migrate in three processes at once.
echo "Applying migrations..."
python manage.py migrate --noinput

python manage.py collectstatic --no-input --clear
python manage.py compilemessages

cat <<EOF | python manage.py shell || echo "superuser bootstrap skipped"
from django.contrib.auth import get_user_model

User = get_user_model()  # get the currently active user model,

User.objects.filter(username='$ADMIN_USER').exists() or \
    User.objects.create_superuser('$ADMIN_USER', '$ADMIN_USER_EMAIL', '$ADMIN_USER_PASSWORD')
EOF
else
WAIT_SECS=${MIGRATE_WAIT_SECS:-300}
echo "Waiting until web has applied migrations..."
deadline=$(( $(date +%s) + WAIT_SECS ))
last_check=""
while true; do
  if last_check=$(python manage.py migrate --check 2>&1); then
    echo "Schema is up to date"
    break
  fi
  if [ "$(date +%s)" -ge "$deadline" ]; then
    echo "Migrations not applied after ${WAIT_SECS}s. Last migrate --check:" >&2
    printf '%s\n' "$last_check" >&2
    exit 1
  fi
  sleep 2
done
fi

exec "$@"
