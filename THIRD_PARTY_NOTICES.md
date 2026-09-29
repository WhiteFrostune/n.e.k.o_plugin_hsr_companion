# 第三方数据与许可说明

## Mar-7th/StarRailRes

- 项目：https://github.com/Mar-7th/StarRailRes
- 固定提交：`541e1100dcfe9a299c6bd500c6d3c4115e0451e4`
- 上游许可：GNU Affero General Public License v3.0 only（AGPL-3.0-only）
- 许可全文：https://github.com/Mar-7th/StarRailRes/blob/541e1100dcfe9a299c6bd500c6d3c4115e0451e4/LICENSE
- 随附许可证全文：`resources/licenses/StarRailRes-AGPL-3.0.txt`

插件内的 `resources/character_registry.json` 是从固定提交的
`index_min/cn/characters.json` 派生的最小角色身份表。它只保留角色 ID、中文名称、
检索标签和人工补充的常见中文别名，并合并“三月七”和“开拓者”的多命途形态。

0.8 的完整资料不随插件包分发。`scripts/build_data_pack.py` 会从同一固定提交读取：

- `characters.json`
- `character_skills.json`
- `character_ranks.json`
- `character_skill_trees.json`
- `character_promotions.json`
- `light_cones.json`
- `light_cone_ranks.json`
- `light_cone_promotions.json`
- `relic_sets.json`
- `items.json`
- `paths.json`
- `elements.json`

这些结构化文本会被转换成独立的 SQLite 资料组件，保存在 N.E.K.O 本机数据目录，
同时保存来源、固定提交、AGPL 许可副本、Schema 版本和 SHA-256。资料组件不包含上游
图像、图标、音频或角色立绘。将资料组件放在插件外部只是为控制插件包体和便于独立
更新，不改变或规避上游许可义务；复制或再分发该资料组件时仍须遵守 AGPL-3.0-only。

上游许可仅覆盖贡献者有权许可的部分，并不授予《崩坏：星穹铁道》的游戏内容、角色
形象、商标或其他知识产权。相关权利归米哈游/HoYoverse 等权利人所有。

## 官方复核参考

- HoYoLAB《崩坏：星穹铁道》官方 Wiki：https://wiki.hoyolab.com/pc/hsr/
- 《崩坏：星穹铁道》官方网站：https://hsr.hoyoverse.com/
- 4.6 版本更新说明：https://hsr.hoyoverse.com/zh-cn/news/166468

官方页面只用于名称和实装状态的人工复核。本插件不复制官方 Wiki 正文、攻略正文或
媒体素材。
