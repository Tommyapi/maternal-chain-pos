#!/bin/bash
# 外部探测：走公网域名经 nginx 打 /shouyin/healthz。连续 3 次失败就重启服务并记录；恢复后记一条。
# cron: */5 * * * *（由 release.sh 安装）
URL=${PROBE_URL:-https://www.example.com/shouyin/healthz}
STATE=/opt/chainpos/backup/.probe_fails
fails=$(cat $STATE 2>/dev/null || echo 0)
if curl -fsS --max-time 10 "$URL" >/dev/null 2>&1; then
  if [ "$fails" != "0" ]; then echo "$(date '+%F %T') 恢复正常（此前连续失败 $fails 次）"; fi
  echo 0 > $STATE
  exit 0
fi
fails=$((fails + 1)); echo $fails > $STATE
echo "$(date '+%F %T') 探测失败 第 $fails 次：$URL"
if [ $fails -ge 3 ]; then
  echo "$(date '+%F %T') 连续 3 次失败，重启 chainpos"
  systemctl restart chainpos
  echo 0 > $STATE
fi
