# hzsz_git_plugin

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)

Home Assistant 自定义集成，通过 Git 仓库一键下载并安装组件到 `custom_components` 目录。

## 功能

- **两步骤配置向导** — 第一步输入 Git 仓库地址和认证信息，第二步从下拉列表选择分支或标签
- **支持公开 / 私有仓库** — HTTP(S) 仓库支持用户名 + 密码 / Token 认证
- **分支 / 标签选择** — 自动获取远程仓库的所有分支和标签，支持选择特定版本或使用默认分支
- **多仓库支持** — 可添加多个配置条目，每个条目对应一个 Git 仓库
- **一键重新下载** — 提供 `hzsz_git_plugin.install` 服务，支持自动化触发
- **安装后自动提示重启** — 通过 Home Assistant 修复（Repairs）机制引导用户重启加载新组件
- **HACS 兼容** — 可直接通过 HACS 自定义仓库安装

## 安装

### 方式一：HACS（推荐）

1. 在 HACS 中添加自定义仓库：`https://github.com/smy188/hzsz_git_installer_components`
2. 搜索 `hzsz_git_plugin` 并下载
3. 重启 Home Assistant

### 方式二：手动安装

1. 将 `custom_components/hzsz_git_plugin/` 目录复制到 Home Assistant 的 `custom_components/hzsz_git_plugin/`
2. 重启 Home Assistant

## 依赖

本集成依赖 [GitPython](https://github.com/gitpython-developers/GitPython)，HACS / Home Assistant 会自动安装。

## 使用方法

### 添加配置

1. 进入 **设置 → 设备与服务 → 添加集成**
2. 搜索 `hzsz_git_plugin`
3. **第 1 步：连接 Git 仓库**
   - 输入 Git 仓库地址（支持 `http://`、`https://`、`git@` 开头）
   - 私有仓库可选填用户名和密码 / Token
4. **第 2 步：选择版本**
   - 从下拉列表中选择分支或标签，或使用仓库默认分支
   - 可选择是否覆盖已存在的同名组件

### 配置参数

| 参数 | 说明 |
|------|------|
| `repo_url` | Git 仓库地址，必填 |
| `username` | 用户名，私有仓库可选 |
| `password` | 密码 / Token，私有仓库可选 |
| `branch` | 选择的分支或标签 |
| `delete_existing` | 是否覆盖已存在的同名组件 |

### 服务调用

```yaml
service: hzsz_git_plugin.install
data:
  entry_id: ""  # 留空安装所有配置的仓库，或指定某个 entry_id
```

## 工作原理

```
┌──────────────┬────────────────────┬───────────────────────────┐
│  Step 1      │  repo_url + auth   │  git ls-remote → refs     │
│  Step 2      │  select ref        │  默认分支 / 分支 / 标签     │
│  Install     │  git clone --depth 1 → cp 到 custom_components  │
└──────────────┴────────────────────┴───────────────────────────┘
```

1. 通过 `git ls-remote` 获取远程仓库的分支和标签列表
2. 用户选择目标版本后，使用 `git clone --depth 1` 浅克隆到临时目录
3. 将临时目录中的所有内容（`.git` 除外）复制到 `custom_components/`
4. 清理临时目录，通过修复系统提示用户重启

## 许可证

MIT
