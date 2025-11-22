# Multi Warehouse Action 使用指南

## 功能概览
- 自动抓取 `https://t.v.nxog.top/apia?id=1` 的多仓入口，并保留 `🌹t主仓库🌹` 等原生线路。
- 自动同步青柠（QingNing）仓库公开 README 中的单仓地址，将其归一为一个名为 `🌹warehouse🌹` 的多仓入口。
- 输出两层静态路由：
  1. `dist/routes/multi/index.json`：多仓索引，可直接分享或在 Tvbox/Takagen 等客户端中引用。
  2. `dist/routes/storehouses/**/*.json`：每个仓库的 `urls` 列表，其中的 `url` 指向原始单仓或 CDN。
- `dist/meta/routes_summary.json` 中包含 `cdn_index` 字段，提供 `index.json` 的 jsDelivr CDN 地址，方便在国内环境下快速访问。

## 本地运行
```bash
pip install -r requirements.txt
python src/fetch_multi.py \
  --config config/routes.yaml \
  --public-repo <GitHubUser>/<RepoName> \
  --public-branch main
```
- `--public-repo` / `--public-branch` 用于生成 Raw/jsDelivr/ghproxy 等地址，保证脚本在本地运行时就能输出正确的 CDN 链接。
- 每次执行脚本都会重新抓取 QingNing README，并把解析到的单仓写入 `data/qingning_single.json`，无需手动维护。

## GitHub Action
仓库自带 `.github/workflows/fetch.yml`：
1. `workflow_dispatch` + 每小时定时触发。
2. Checkout → 安装依赖 → 执行同样的 `python src/fetch_multi.py` → 上传并提交 `dist/`。
3. 运行完成后，可直接使用以下地址：
   - 入口：`https://raw.githubusercontent.com/<repo>/main/dist/routes/multi/index.json`
   - jsDelivr：参考 `dist/meta/routes_summary.json` 的 `cdn_index`

## 定制方式
- 修改 `config/routes.yaml` 中的 `pipelines` 以新增或删减仓库；新增其它多仓时使用 `remote_storehouse` 或 `local_urls_storehouse` 即可。
- `qingning_remote` 节点可以替换 `urls` 列表或改变 `single_name_template`，以确保单仓名称符合自身规范。
- 如果需要关闭 QingNing 自动同步，可将 `qingning_remote.enabled` 设为 `false`，改为手动维护 `data/qingning_single.json`。

完成以上配置后，只需 push 至 GitHub 并启用 Action，即可拥有一个自动刷新、支持 jsDelivr CDN 的多仓路由项目。 
