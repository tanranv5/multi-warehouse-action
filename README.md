# Multi Warehouse Action 使用指南

## GitHub Action
仓库自带 `.github/workflows/fetch.yml`：
1. `workflow_dispatch` + 每小时定时触发。
2. Checkout → 安装依赖 → 执行同样的 `python src/fetch_multi.py` → 上传并提交 `dist/`。
3. 运行完成后，可直接使用以下地址：
   - 入口：`https://raw.githubusercontent.com/<repo>/main/dist/routes/multi/index.json`
   - jsDelivr：参考 `dist/meta/routes_summary.json` 的 `cdn_index`

完成以上配置后，只需 push 至 GitHub 并启用 Action，即可拥有一个自动刷新、支持 jsDelivr CDN 的多仓路由项目。 

## QingNing 校验说明
- `config/routes.yaml` 的 `qingning_remote` 节点会在每次运行时抓取青柠 README，整理出单仓列表，并通过 `single_name_template` 统一命名。
- 新增的 `validation` 配置会逐一请求这些单仓（默认超时 10 秒、必须返回 JSON），无法访问的地址会被跳过，从而避免把失效单仓写进 `dist/routes/storehouses/qingning/warehouse.json`。
- 如果需要放宽要求，可以把 `validation.require_json` 设为 `false` 或增大 `timeout`/`max_count`；若完全不想校验，将 `validation.enabled` 设为 `false` 即可。
