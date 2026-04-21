#!/bin/bash
Q='sum by (host, container_name) (count_over_time({source="docker"} != "caller=scheduler_processor.go" != "caller=retry.go" != "caller=metrics.go" != "caller=engine.go" != "caller=ingester.go" != "App runner exited without error" !~ "logger=(ngalert|provisioning|infra\\.usagestats)" | logfmt | level=~"(?i)error|fatal|panic" != "context canceled" [5m]))'
curl -sG http://localhost:3100/loki/api/v1/query --data-urlencode "query=$Q"
