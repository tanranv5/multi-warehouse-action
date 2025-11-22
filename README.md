# Multi Warehouse Action 使用指南

## GitHub Action
仓库自带 `.github/workflows/fetch.yml`：
1. `workflow_dispatch` + 每小时定时触发。
2. Checkout → 安装依赖 → 执行同样的 `python src/fetch_multi.py` → 上传并提交 `dist/`。
3. 运行完成后，可直接使用以下地址：
   - 入口：`https://raw.githubusercontent.com/<repo>/main/dist/routes/multi/index.json`
   - jsDelivr：参考 `dist/meta/routes_summary.json` 的 `cdn_index`

完成以上配置后，只需 push 至 GitHub 并启用 Action，即可拥有一个自动刷新、支持 jsDelivr CDN 的多仓路由项目。 
