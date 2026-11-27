#!/bin/bash
# Move the DM agent to a Linux server (Ubuntu or Debian). Log in as root or as a
# user with passwordless sudo (the default on Google Cloud, AWS and Azure).
#
#   deploy/deploy.sh push    root@1.2.3.4   copy everything, install the timer (not started), dry run
#   deploy/deploy.sh dryrun  root@1.2.3.4   one dry run on the server, sends nothing
#   deploy/deploy.sh golive  root@1.2.3.4   stop the Mac job, copy sent.db, start the server timer
#   deploy/deploy.sh update  root@1.2.3.4   copy agent.py and replies/ only (after editing a reply)
#   deploy/deploy.sh status  root@1.2.3.4   timer state, last log lines, last sends
#   deploy/deploy.sh back    root@1.2.3.4   stop the server, copy sent.db home, restart the Mac job
#
# Only one copy may be live at a time. Two copies would both answer DM keywords.
set -euo pipefail

cmd="${1:-}"
host="${2:-}"
if [ -z "$cmd" ] || [ -z "$host" ]; then
  sed -n '2,12p' "$0"
  exit 1
fi

cd "$(dirname "$0")/.."
dir=/opt/dm-agent
label=local.dm-agent
plist="$HOME/Library/LaunchAgents/$label.plist"
export COPYFILE_DISABLE=1   # keep macOS "._" files out of replies/

remote() { ssh "$host" "sudo sh -c '$*'"; }   # commands must not contain single quotes
as_agent() { remote "cd $dir && runuser -u dmagent -- $*"; }

copy_code() {
  remote "rm -rf $dir/replies"
  tar czf - agent.py replies | remote "tar xzf - --no-same-owner -C $dir && chown -R dmagent: $dir"
}

copy_state() {
  # .env holds the token: owner-only on the server too.
  tar czf - .env sent.db | remote "tar xzf - --no-same-owner -C $dir && chown dmagent: $dir/.env $dir/sent.db && chmod 600 $dir/.env"
}

mac_job_stop() {
  # Wait for a pass in progress to finish, so no send is cut off before it is logged.
  while launchctl print "gui/$(id -u)/$label" 2>/dev/null | grep -q "state = running"; do sleep 2; done
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
}

case "$cmd" in
  push)
    remote "id dmagent >/dev/null 2>&1 || useradd --system --home $dir --shell /usr/sbin/nologin dmagent; mkdir -p $dir; python3 --version"
    copy_code
    copy_state
    tar czf - -C deploy dm-agent.service dm-agent.timer | remote "tar xzf - --no-same-owner -C /etc/systemd/system && systemctl daemon-reload"
    echo "Copied. The timer is NOT started. Dry run on the server:"
    as_agent "python3 agent.py check"
    as_agent "env DRY_RUN=1 python3 agent.py run"
    ;;
  dryrun)
    as_agent "env DRY_RUN=1 python3 agent.py run"
    ;;
  golive)
    mac_job_stop
    echo "Mac job stopped."
    copy_code
    copy_state
    remote "systemctl enable --now dm-agent.timer && systemctl start dm-agent.service; tail -3 $dir/agent.log"
    echo "Server is live. The Mac job stays off."
    ;;
  update)
    copy_code
    as_agent "python3 agent.py check"
    ;;
  status)
    remote "systemctl is-active dm-agent.timer; systemctl list-timers dm-agent.timer --no-pager | sed -n 1,2p; tail -15 $dir/agent.log"
    as_agent "python3 agent.py log"
    ;;
  back)
    remote "systemctl disable --now dm-agent.timer; while systemctl is-active -q dm-agent.service; do sleep 2; done"
    remote "cat $dir/sent.db" > sent.db.new
    [ -s sent.db.new ] && mv sent.db.new sent.db
    launchctl bootstrap "gui/$(id -u)" "$plist"
    echo "Server stopped. Mac job running again."
    ;;
  *)
    sed -n '2,12p' "$0"
    exit 1
    ;;
esac
