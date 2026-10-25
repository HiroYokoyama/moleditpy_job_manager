#!/usr/bin/env bash
set -euo pipefail
scheduler=${1:?scheduler required}
: "${LAB:?LAB must point at disposable runner storage}"
mkdir -p "$LAB"
host=$(hostname -s)
export DEBIAN_FRONTEND=noninteractive
sudo apt-get update -qq
sudo -E apt-get install -y --no-install-recommends openssh-server python3

case "$scheduler" in
  slurm)
    sudo -E apt-get install -y --no-install-recommends slurm-wlm munge
    sudo install -d -o slurm -g slurm /var/spool/slurmctld
    sudo mkdir -p /var/spool/slurmd
    sudo tee /etc/slurm/slurm.conf >/dev/null <<EOF
ClusterName=moleditpy-lab
SlurmctldHost=$host
SlurmUser=slurm
AuthType=auth/munge
StateSaveLocation=/var/spool/slurmctld
SlurmdSpoolDir=/var/spool/slurmd
SlurmctldPidFile=/run/slurmctld.pid
SlurmdPidFile=/run/slurmd.pid
ProctrackType=proctrack/pgid
TaskPlugin=task/none
JobAcctGatherType=jobacct_gather/none
SelectType=select/cons_tres
SelectTypeParameters=CR_CPU
SchedulerType=sched/backfill
SchedulerParameters=sched_interval=1,bf_interval=1
SlurmdParameters=config_overrides
ReturnToService=2
SlurmctldTimeout=30
SlurmdTimeout=30
NodeName=$host CPUs=2 RealMemory=1024 State=UNKNOWN
PartitionName=debug Nodes=$host Default=YES MaxTime=00:05:00 State=UP
EOF
    sudo systemctl restart munge slurmctld slurmd
    sudo scontrol update "NodeName=$host" State=RESUME || true
    sinfo
    sbatch --version
    ;;
  sge)
    printf 'gridengine-common shared/gridenginemaster string %s\n' "$host" | sudo debconf-set-selections
    printf 'gridengine-common shared/gridenginecell string default\n' | sudo debconf-set-selections
    sudo -E apt-get install -y --no-install-recommends gridengine-master gridengine-exec gridengine-client
    sudo service gridengine-master restart
    for attempt in $(seq 1 30); do sudo qconf -ss && break; sleep 1; done
    sudo qconf -ss | grep -Fx "$host" || sudo qconf -as "$host"
    if ! sudo qconf -sel | grep -Fx "$host"; then
      sudo qconf -se global | sed "s/^hostname .*/hostname $host/" > "$LAB/exec-host"
      sudo qconf -Ae "$LAB/exec-host"
    fi
    sudo qconf -mattr queue hostlist "$host" all.q
    sudo qconf -mattr queue slots 2 all.q
    sudo qconf -mattr queue load_thresholds NONE all.q
    sudo qconf -mattr queue shell /bin/bash all.q
    cat > "$LAB/smp-pe" <<'EOF'
pe_name smp
slots 2
user_lists NONE
xuser_lists NONE
start_proc_args /bin/true
stop_proc_args /bin/true
allocation_rule $pe_slots
control_slaves FALSE
job_is_first_task TRUE
urgency_slots min
accounting_summary FALSE
EOF
    sudo qconf -Ap "$LAB/smp-pe"
    sudo qconf -mattr queue pe_list smp all.q
    sudo qconf -ssconf | sed 's/^schedule_interval .*/schedule_interval 00:00:01/' > "$LAB/sched-conf"
    sudo qconf -Msconf "$LAB/sched-conf"
    sudo service gridengine-exec restart
    sudo qmod -e all.q
    qstat -f
    qsub -help 2>&1 | head -n 2 || true
    ;;
  pbs)
    sudo -E apt-get install -y --no-install-recommends gcc g++ make libtool libhwloc-dev \
      libx11-dev libxt-dev libedit-dev libical-dev libncurses-dev perl \
      postgresql-server-dev-14 postgresql-contrib-14 postgresql-14 python3-dev tcl-dev tk-dev swig \
      libexpat1-dev libssl-dev libxext-dev libxft-dev autoconf automake pkg-config
    # OpenPBS v23.06.06, pinned rather than following a mutable branch.
    git init "$LAB/openpbs-source"
    git -C "$LAB/openpbs-source" remote add origin https://github.com/openpbs/openpbs.git
    git -C "$LAB/openpbs-source" fetch --depth 1 origin be361a18ef37652bece0811ec709f0fe4fc52710
    git -C "$LAB/openpbs-source" checkout --detach FETCH_HEAD
    (
      cd "$LAB/openpbs-source"
      ./autogen.sh
      ./configure --prefix=/opt/pbs
      make -j2
      sudo make install
    )
    sudo /opt/pbs/libexec/pbs_postinstall
    sudo tee /etc/pbs.conf >/dev/null <<EOF
PBS_EXEC=/opt/pbs
PBS_SERVER=$host
PBS_START_SERVER=1
PBS_START_SCHED=1
PBS_START_COMM=1
PBS_START_MOM=1
PBS_HOME=/var/spool/pbs
PBS_CORE_LIMIT=unlimited
PBS_SCP=/usr/bin/scp
PBS_DB_EXEC=/usr/lib/postgresql/14
EOF
    sudo chmod 4755 /opt/pbs/sbin/pbs_iff /opt/pbs/sbin/pbs_rcp
    sudo /etc/init.d/pbs start
    for attempt in $(seq 1 45); do sudo /opt/pbs/bin/qmgr -c 'print server' && break; sleep 1; done
    sudo /opt/pbs/bin/qmgr -c "create node $host"
    sudo /opt/pbs/bin/qmgr -c 'set server scheduling = True'
    sudo /opt/pbs/bin/qmgr -c 'set server scheduler_iteration = 1'
    sudo /opt/pbs/bin/qmgr -c 'set server job_history_enable = True'
    sudo /opt/pbs/bin/qmgr -c 'set server job_history_duration = 00:02:00'
    /opt/pbs/bin/qstat --version
    ;;
  *) echo "Unknown scheduler: $scheduler" >&2; exit 2 ;;
esac

# A dedicated account, key and loopback-only sshd in the disposable VM.
sudo useradd --create-home --shell /bin/bash schedulerlab
sudo passwd -d schedulerlab >/dev/null
ssh-keygen -q -t ed25519 -N '' -f "$LAB/id_ed25519"
ssh-keygen -q -t ed25519 -N '' -f "$LAB/ssh_host_ed25519_key"
sudo install -d -m 700 -o schedulerlab -g schedulerlab /home/schedulerlab/.ssh
sudo install -m 600 -o schedulerlab -g schedulerlab "$LAB/id_ed25519.pub" /home/schedulerlab/.ssh/authorized_keys
cat > "$LAB/sshd_config" <<EOF
Port 2222
ListenAddress 127.0.0.1
HostKey $LAB/ssh_host_ed25519_key
PidFile $LAB/sshd.pid
AuthorizedKeysFile .ssh/authorized_keys
PubkeyAuthentication yes
AuthenticationMethods publickey
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
UsePAM no
Subsystem sftp internal-sftp
EOF
sudo mkdir -p /run/sshd
sudo /usr/sbin/sshd -f "$LAB/sshd_config" -E "$LAB/sshd.log"
ssh-keyscan -p 2222 127.0.0.1 > "$LAB/known_hosts" 2>/dev/null
