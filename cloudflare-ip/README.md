# Cloudflare IP 每日整合

纯 Python 脚本在 GitHub Actions 后台运行，从两个公开来源获取 IPv4，合并去重后生成仓库根目录的 `cloudflare.txt`。

```text
172.64.229.79:443
172.64.229.71:443
```

**默认每天北京时间 07:17 更新一次。** 不启动浏览器、不安装 Playwright、不需要第三方 Python 包或个人访问令牌。正常脚本执行无输出，失败时输出错误并返回非零状态；GitHub Actions 自身仍会保留运行记录和日志。

## 创建 GitHub 仓库并上传

1. 登录 GitHub，点击右上角 **+ → New repository**，仓库名可用 `cloudflare-ip`。如果需要无需登录即可读取 TXT，选择 **Public**；然后点击 **Create repository**。
2. 解压本代码包。把解压后的内容上传到仓库根目录，目录结构应如下。ZIP 文件本身不需要上传。

   ```text
   .github/workflows/update-ips.yml
   .gitignore
   README.md
   scripts/update_ips.py
   tests/test_update_ips.py
   ```

   注意 `.github` 和 `.gitignore` 以点开头。网页上传时请确认 **.github/workflows/update-ips.yml** 也已上传；可将 `.github` 文件夹拖入上传区域。若遗漏，使用 **Add file → Create new file**，文件名填写 `.github/workflows/update-ips.yml`，复制本包中的 YAML 内容并保存。不要在仓库中额外套一层 `cloudflare-ip` 文件夹。
3. 打开仓库的 **Actions** 标签。如 GitHub 提示启用工作流，先启用；选择 **Update Cloudflare IPs → Run workflow → Run workflow**，手动执行第一次。
4. 等待运行成功，返回仓库根目录，即可看到自动生成的 `cloudflare.txt`。之后工作流会按日运行，只有内容变化时才产生新的提交。

若 `git push` 报 `403` 或只读权限，请检查 **Settings → Actions → General → Workflow permissions**。允许工作流写入仓库，并保留 YAML 中的 `contents: write`。组织权限限制或分支保护也可能阻止机器人直接推送，需要仓库管理员调整相应规则。

## 获取 TXT 地址

打开 `cloudflare.txt`，点击 **Raw**，复制其链接。公开仓库一般为：

```text
https://raw.githubusercontent.com/你的用户名/你的仓库名/main/cloudflare.txt
```

如果默认分支不是 `main`，请将它替换为实际默认分支名。私有仓库的 Raw 文件需要认证，不适合直接给其他程序作为公开下载地址。

本代码包未附过期名单作为初始 `cloudflare.txt`；文件由首次成功运行生成。

## 修改更新频率

编辑 `.github/workflows/update-ips.yml` 中的 `cron`。本配置采用 UTC，北京时间比 UTC 快 8 小时：

| 更新频率 | cron | 北京时间 |
| --- | --- | --- |
| 每天一次（默认） | `17 23 * * *` | 每天 07:17 |
| 每天两次 | `17 3,15 * * *` | 每天 11:17、23:17 |
| 每 6 小时一次 | `17 */6 * * *` | 每天 02:17、08:17、14:17、20:17 |
| 每周一次 | `17 23 * * 0` | 每周一 07:17 |

GitHub 定时任务按默认分支中的配置运行，可能因平台负载延迟，不能保证精确到分钟。公开仓库长时间（通常 60 天）无活动时，GitHub 可能停用定时任务；到 Actions 页面重新启用即可。[GitHub 官方说明](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)

## 两个来源如何处理

- [ipTop10](https://ip.164746.xyz/ipTop10.html)：将逗号分隔的列表转为 IPv4 地址。
- [麒麟 Cloudflare 页面](https://api.uouin.com/cloudflare.html)：读取页面中 `cloudflare.js?id=...` 的当前 `id`，生成新的 `time/key`，然后请求动态 JSON 接口。取电信（`ctcc`）、联通（`cucc`）、移动（`cmcc`）、多线（`bgp`）的 `info[].ip`。

只收集 IPv4，统一添加 `:443`，按第一来源、第二来源电信/联通/移动/多线的顺序去重。保留上游排列顺序，不进行额外测速；端口 443 是输出规则，不表示脚本已验证每个地址的连通性。

**失败保护：** 任一来源请求失败、接口报错、任一 IPv4 分类为空、数据包含非法 IP 时，最多重试 3 次。仍失败则保留旧 TXT，Actions 标记失败，避免发布空名单或缺少来源的不完整名单。默认单次 HTTP 超时 25 秒。第二来源重试时会重新获取页面、`id` 和毫秒时间戳；Cookie 自动保持，临时 key、Cookie 不保存到仓库。

签名公式由用户提供的网页脚本确认：`MD5(MD5(A) + B + MD5(id) + time)`，`time` 使用十进制毫秒字符串。A/B 是该页面脚本中的公开签名常量，存放在 Python 文件的 `SIGN_TEXT` / `SIGN_SEPARATOR`。变化的 `id` 自动读取；若网站以后修改签名公式或 A/B，需要同步修改代码。不要把一条历史接口 URL 中的 key/time 固定到配置里，也不要用页面源码内置的旧静态表格代替动态接口数据。

## 本地执行和测试

在仓库根目录使用 Python 3.12 或更新版本执行：

```shell
python scripts/update_ips.py
```

指定文件位置：

```shell
python scripts/update_ips.py --output result.txt
```

离线测试（不访问外部网站）：

```shell
python -m unittest discover -s tests -v
```

测试覆盖两组真实请求的签名向量、动态 `id`、IPv4 校验、四线路解析、去重、有限重试、请求参数、失败保留原文件、原子替换和正常静默执行。

## 当前验证情况

网页脚本中的签名规则已与两组真实请求参数逐一比对；离线测试已通过。代码交付环境的 DNS/联网限制阻止了纯 HTTP 脚本的真实外网端到端运行，尚未创建 GitHub 仓库或验证实际 Actions 执行。请按上面的步骤手动执行第一次，确认两个来源都可被 GitHub runner 访问；失败时查看 **Fetch and merge IPs** 步骤的错误。本包没有把离线通过表述为实际 GitHub 部署成功。
