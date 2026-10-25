#!/usr/bin/env bash
set +e
case "${1:-}" in
  slurm)
    sinfo -l
    squeue -l
    scontrol show nodes
    sudo journalctl -u slurmctld -u slurmd --no-pager -n 180
    ;;
  sge)
    qstat -f
    qconf -ss
    qconf -sel
    qconf -sq all.q
    sudo journalctl -u gridengine-master -u gridengine-exec --no-pager -n 180
    sudo find /var/spool/gridengine /var/lib/gridengine -name messages -type f -exec tail -n 60 {} \; 2>/dev/null
    ;;
  pbs)
    /opt/pbs/bin/qstat -an
    sudo /opt/pbs/bin/pbsnodes -av
    sudo /opt/pbs/bin/qmgr -c 'print server'
    sudo find /var/spool/pbs/server_logs /var/spool/pbs/mom_logs /var/spool/pbs/sched_logs -type f -exec tail -n 60 {} \; 2>/dev/null
    ;;
esac
exit 0
