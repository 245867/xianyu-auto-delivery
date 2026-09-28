# 前端（React + Vite）

闲鱼智控 Pro 的管理后台前端。技术栈：React 19、TypeScript、Vite 6、Tailwind CSS 3、Recharts。

## 目录说明

```
frontend/
├── App.tsx              # 应用入口与页面路由
├── index.tsx            # React 挂载点
├── components/          # 各功能页面组件
├── services/api.ts      # 后端接口封装
├── lib/request.ts       # axios 请求实例与拦截器
├── types.ts             # 全局类型定义
└── vite.config.ts       # Vite 配置
```

## 开发

```bash
npm install
npm run dev
```

开发服务器会把 `/api`、`/login` 等请求代理到后端 `http://localhost:8080`（见 `vite.config.ts`），因此请先启动后端。

## 构建

```bash
npm run build
```

构建产物直接输出到仓库根目录的 `static/`（`build.outDir = '../static'`），由后端以静态资源方式托管。由于 `static/` 中还包含后端依赖的 `xianyu_js_version_2.js`，`emptyOutDir` 被设为 `false`，构建不会清空该目录。

构建完成后，`static/assets/` 下会生成带内容哈希的文件名，并自动更新 `static/index.html` 的引用。旧的哈希文件即为失效产物，可以删除。

> 注意：`vite.config.ts` 中 `sourcemap: true` 会额外生成体积很大的 `.map` 文件，且会暴露前端源码。这些文件已在根目录 `.gitignore` 中排除，不会进入仓库。

## 类型检查

```bash
npx tsc --noEmit
```
