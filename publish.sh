#!/bin/bash
# 一键构建并发布到 PyPI
# 用法: bash publish.sh [version]
# 示例: bash publish.sh 1.0.1

set -e

if [ -n "$1" ]; then
  # 更新版本号
  sed -i '' "s/^version = .*/version = \"$1\"/" pyproject.toml
  sed -i '' "s/__version__ = .*/__version__ = \"$1\"/" citationclaw/__init__.py
  echo "Version updated to $1"
fi

# 清理旧构建
rm -rf dist/ build/ *.egg-info

# 安装构建工具（如未安装）
pip install --quiet build twine

# 构建
python -m build

# 上传到 PyPI
twine upload dist/*

echo ""
echo "✅ Published! Install with: pip install citationclaw"
