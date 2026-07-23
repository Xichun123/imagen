# Imagen Skill

通过预配置的图像 API provider 生成或编辑 raster 图片，支持运行时切换 provider/model、批量预检、原子输出和可选 downscale。

## 仓库结构

本仓库采用 `skills/<skill-name>` 布局；可安装的 Skill 本体位于 [`skills/imagen/`](skills/imagen/)。

```text
skills/imagen/
├── SKILL.md
├── scripts/imagen.py
├── references/
├── tests/
├── providers.example.json
├── providers.schema.json
└── pyproject.toml
```

## 安装

使用 skills CLI 安装到所有支持的 Agent：

```bash
skills add Xichun123/imagen --skill imagen -g -a universal -y
```

手动安装：

```bash
cp -R skills/imagen ~/.agents/skills/imagen
cd ~/.agents/skills/imagen
python -m pip install '.[downscale]'
```

安装前请审查 Skill 内容，并按本地环境的依赖审批流程操作。

## Provider 配置

```bash
mkdir -p ~/.config/imagen
cp skills/imagen/providers.example.json ~/.config/imagen/providers.json
# 编辑 provider URL、模型及 api_key_env（推荐）或 api_key 后：
python skills/imagen/scripts/imagen.py config-check
```

完整说明见 [`skills/imagen/references/provider-config.md`](skills/imagen/references/provider-config.md)。Google Gemini `generateContent` 配置见 [`skills/imagen/references/google-generate-content.md`](skills/imagen/references/google-generate-content.md)。推荐通过环境变量或 secret manager 提供 key；若使用直接 `api_key`，不要提交本地配置文件。

## 使用

```bash
python skills/imagen/scripts/imagen.py providers
python skills/imagen/scripts/imagen.py generate \
  --prompt "A cozy alpine cabin at dawn" \
  --out output/imagen/cabin.png

# Google Gemini generateContent provider
python skills/imagen/scripts/imagen.py generate \
  --provider google \
  --prompt "A cinematic alpine cabin at dawn" \
  --aspect-ratio 16:9 \
  --image-size 2K \
  --out output/imagen/google-cabin.png
```

## 开发验证

```bash
python -m pip install './skills/imagen[test,downscale]'
python -m unittest discover -s skills/imagen/tests -v
python -m py_compile skills/imagen/scripts/imagen.py
```

## License

[Apache-2.0](LICENSE.txt)
