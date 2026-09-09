# Conch CI

| Workflow | 用途 |
| --- | --- |
| `prepare-self-hosted-runner.yml` | 检查或安装自托管 Runner 所需的锁定工具环境。 |
| `build-and-check.yml` | 构建 Conch，并运行静态检查、Go 测试、Go vet、Python SDK 与 Conch-ci 运行时清理单元测试。 |
| `template-image-integration.yml` | 使用本地 OCI registry 和隔离 conchd 验证 Image/Template 重定向、同名隔离、类型门禁及完整生命周期。 |
| `network-pool-integration.yml` | 验证网络池预填、失败重试和资源清理。 |
| `conchd-crash-release.yml` | 验证 `conchd` 异常退出并重启后能清理遗留资源，并复用同一 Sandbox ID。 |
| `conch-init-smoke.yml` | 在真实虚拟机中验证 `conch-init` 启动、vsock 就绪和 SDK 健康检查。 |
| `e2b-template-weekly.yml` | 定期构建或复用内核、RootFS 和 E2B Template，手动运行时可选发布到 GHCR。 |
| `e2b-workload-smoke.yml` | 验证 E2B SDK、虚拟机连通性、网络策略和网络槽复用。 |
| `sync-conch.yml` | 将 AtomGit 的 `dev` 分支和 Pull Request 同步到 GitHub，不运行测试。 |

自托管任务以 `self-hosted, Linux, Huawei` 为基础标签，支持 ARM64（aarch64）和
X64（x86_64）；当前宿主系统允许 openEuler 24.03 LTS-SP3 或 Ubuntu 26.04 LTS。
Ubuntu 使用 `ID` 和 `VERSION_ID` 判断兼容性，点版本的 `PRETTY_NAME` 变化不会
导致平台拒绝或验证失败；显示名称仍记录在回执中，执行 `ensure` 时可更新。
openEuler 仍保留精确的 LTS-SP3 校验。
内核生产任务可以调度到任一架构，下游任务跟随生产者的架构，避免把 ARM64 内核或
模板交给 X64 Runner。RootFS 和工具下载使用对应的 `arm64` / `amd64` 平台。

RootFS 和 Template 仍通过生产机器的 `localhost:5001` registry 按 digest 消费。
当前部署每种架构各一台 Runner；消费者会校验生产者的 Runner 名称。若增加同架构
Runner，需要先实现镜像跨机器传输或合并生产/消费任务；当前会拒绝跨机器消费，
不会假定另一台机器拥有相同的本地镜像。

机器初始化必须预先提供 Docker（包括 Buildx、用户可访问 daemon）、可读写的
`/dev/kvm`、已加载的 EROFS 支持、免密 sudo、网络工具及完整编译依赖。
详细前置项见 `scripts/runner-env/runner_env.py` 的 `verify_baseline()`。
CI 不安装或升级宿主机软件包，`verify` 只读检查，只有专用准备工作流执行 `ensure`。
新 Runner 需要先补齐这些前置条件，再运行 `prepare-self-hosted-runner.yml`，选择
`runner_arch: X64` 和 `mode: ensure`；默认 `mode: verify`。
`runner_arch` 也支持 `ARM64` 或 `all`，默认 `all`；相关 main 分支变更会分别为两种
架构执行准备任务。离线架构的任务会等待对应 Runner 上线。

`runner-env.lock.yaml` schema 2 为二进制组件分别锁定 ARM64/AMD64 的 SHA-256，
源码构建的 erofs-utils 共用源码校验和。内核构建 ID v3 包含源码、配置、架构和
构建脚本摘要，本次更新会失效旧内核缓存；RootFS 和 Template 缓存也会因配方更新
重新计算。宿主编译器和 BuildKit 实现变更仍不单独影响内核或 RootFS 构建 ID。
更新后的环境 ID 由仓库输入自动计算，两台 Runner 都需要通过准备工作流更新回执。

使用 `start-conchd` action 的自托管任务共享 `conch-ci-conchd-runtime` 并发组。每次
启动会先检查固定的 CNI 配置挂载和 SDK socket 链接；如果它们可验证地属于一个已经
没有存活 `conchd` 的旧 `$RUNNER_TEMP` 运行目录，CI 会记录告警、执行兜底清理并继续
当前任务。所有权不明确或旧 `conchd` 仍存活时，CI 会拒绝接管这些资源。
