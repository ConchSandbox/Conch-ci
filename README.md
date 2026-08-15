# Conch CI

| Workflow | 用途 |
| --- | --- |
| `prepare-self-hosted-runner.yml` | 检查或安装自托管 Runner 所需的锁定工具环境。 |
| `build-and-check.yml` | 构建 Conch，并运行静态检查、Go 测试、Go vet、Python SDK 与 Conch-ci 运行时清理单元测试。 |
| `network-pool-integration.yml` | 验证网络池预填、失败重试和资源清理。 |
| `conchd-crash-release.yml` | 验证 `conchd` 异常退出并重启后能清理遗留资源，并复用同一 Sandbox ID。 |
| `conch-init-smoke.yml` | 在真实虚拟机中验证 `conch-init` 启动、vsock 就绪和 SDK 健康检查。 |
| `e2b-template-weekly.yml` | 定期构建或复用内核、RootFS 和 E2B Template，手动运行时可选发布到 GHCR。 |
| `e2b-workload-smoke.yml` | 验证 E2B SDK、虚拟机连通性、网络策略和网络槽复用。 |
| `sync-conch.yml` | 将 AtomGit 的 `dev` 分支和 Pull Request 同步到 GitHub，不运行测试。 |

使用 `start-conchd` action 的自托管任务共享 `conch-ci-conchd-runtime` 并发组。每次
启动会先检查固定的 CNI 配置挂载和 SDK socket 链接；如果它们可验证地属于一个已经
没有存活 `conchd` 的旧 `$RUNNER_TEMP` 运行目录，CI 会记录告警、执行兜底清理并继续
当前任务。所有权不明确或旧 `conchd` 仍存活时，CI 会拒绝接管这些资源。
