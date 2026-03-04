# Adobe Cookie Exporter 插件

一个 Chrome/Edge（Manifest V3）插件，用于导出 Adobe/Firefly Cookie。
当前改为仅导出 `adobe2api` 导入所需最小字段。

插件界面仅保留：

- 导出范围
- 导出最简 JSON

## 导出格式

导出的 JSON 结构如下（最简）：

```json
{
  "cookie": "k1=v1; k2=v2"
}
```

## 安装方式（开发者模式）

1. 打开 Chrome/Edge 扩展页面：`chrome://extensions` 或 `edge://extensions`
2. 开启「开发者模式」
3. 点击「加载已解压的扩展程序」
4. 选择目录：`browser-cookie-exporter/`

## 使用说明

1. 先在浏览器登录 Adobe/Firefly
2. 点击插件图标
3. 选择导出范围：
   - `Adobe 全域（推荐）`：读取 `*.adobe.com` 相关 Cookie
   - `当前站点`：仅读取当前标签页站点 Cookie
4. 可选填写账号标识（用于文件名和 JSON 的 `email` 字段）
5. 点击 `导出 JSON`

## 与 adobe2api 联动

可直接把导出的 JSON 传给 `adobe2api` 的导入接口：

```bash
curl -X POST "http://127.0.0.1:6001/api/v1/refresh-profiles/import-cookie" \
  -H "Content-Type: application/json" \
  -d '{"name":"my-account","cookie": <导出的整个JSON或cookie_header字符串>}'
```

说明：导出文件名格式为 `cookie_YYYYMMDD_HHMMSS.json`。
