"""Small Chinese presentation labels; native skill names remain unchanged."""
from __future__ import annotations

import re


LABELS = {
    'daily-report-browser-debug': ('业务页面调试', '排查业务页面的显示、点击和浏览器交互问题。'),
    'daily-report-change-check': ('业务改动检查', '检查业务项目的代码改动和受影响行为。'),
    'daily-report-full-tests': ('业务完整测试', '对业务项目运行完整回归检查。'),
    'daily-report-h5-delivery': ('业务 H5 交付', '处理手机业务页面的验证和交付流程。'),
    'daily-report-migration-audit': ('业务迁移核对', '核对业务功能迁移的范围、依赖和遗留。'),
    'daily-report-runtime-check': ('业务运行检查', '检查业务服务的实际运行状态。'),
    'codex-web-readonly-health': ('工作台健康检查', '只读检查工作台服务和运行状态，不发布、不改配置。'),
    'cost-aware-orchestration': ('AI 任务分工', '按任务复杂度安排协作，平衡成本、速度和质量。'),
    'mobile-handoff': ('文件交付到手机', '把需要查看或下载的最终文件交付到手机。'),
    'waytoagi': ('AI 教程知识库', '查找本地通往 AGI 之路的教程和学习资料。'),
    'imagegen': ('生成与编辑图片', '按要求生成图片，或修改已有图片。'),
    'openai-docs': ('OpenAI 官方资料', '查询 Codex、模型和 OpenAI 产品的官方用法。'),
    'skill-creator': ('编写技能', '创建或修改可复用的技能说明。'),
    'skill-installer': ('安装技能', '按明确需求查找和安装技能。'),
    'plugin-creator': ('编写插件', '创建或维护本地 Codex 插件。'),
    'plugin-management': ('插件管理', '查询插件及其权限、依赖和连接状态。'),
    'dingtalk-aisearch': ('钉钉综合搜索', '按姓名、职责等找人，或跨文档、消息和邮件检索。'),
    'dingtalk-aitable': ('钉钉多维表', '查询或维护 AI 表格的字段、记录和数据源。'),
    'dingtalk-calendar': ('钉钉日程与会议室', '查询日程、共同空闲，安排会议或预订会议室。'),
    'dingtalk-chat': ('钉钉消息与群聊', '查找或发送消息，处理群聊和群成员。'),
    'dingtalk-contact': ('钉钉通讯录', '精确查询人员、部门和联系方式。'),
    'dingtalk-doc': ('钉钉文字文档', '读取、编辑或导出在线文字文档。'),
    'dingtalk-drive': ('钉盘与文件', '查找、上传、下载和整理钉钉文件。'),
    'dingtalk-event': ('钉钉事件监听', '按明确要求监听消息、审批、通话或待办变化。'),
    'dingtalk-mail': ('钉钉邮件', '查询、撰写和处理钉钉邮件。'),
    'dingtalk-minutes': ('钉钉 AI 听记', '查询会议听记、逐字稿、摘要和行动项。'),
    'dingtalk-misc': ('钉钉其他业务', '处理审批、考勤、日报、电子表格等其他功能。'),
    'dingtalk-shared': ('钉钉任务导航', '当需求涉及多个钉钉产品时，找到合适的处理方式。'),
    'dingtalk-todo': ('钉钉待办', '查询、创建或更新待办任务。'),
    'dingtalk-wiki': ('钉钉知识库', '查找和整理知识库、空间及库内节点。'),
}
GROUPS = {'project': '当前项目', 'general': '通用能力', 'dingtalk': '钉钉办公',
          'management': '技能与插件维护'}


def skill_choices(catalog, *, include_disabled=False):
    by_name = {}
    for raw in catalog.get('skills', []) if isinstance(catalog, dict) else []:
        if isinstance(raw, dict) and isinstance(raw.get('name'), str) and raw['name'].strip():
            by_name.setdefault(raw['name'].strip(), []).append(raw)
    rows = []
    for name, matches in by_name.items():
        enabled = [item for item in matches if item.get('enabled')]
        if not include_disabled and not enabled:
            continue
        raw = (enabled or matches)[0]
        scope = str(raw.get('scope') or 'unknown').lower()
        description = str(raw.get('description') or '').strip()[:600]
        key = name.rsplit(':', 1)[-1]
        title, usage = LABELS.get(key, (name, description[:160] if re.search(r'[\u4e00-\u9fff]', description)
                                       else '专项工作流程；不确定时保持自动选择，可查看原始说明。'))
        group = ('project' if scope in {'repo', 'project'} else 'dingtalk' if key.startswith('dingtalk-')
                 else 'management' if key in {'skill-creator', 'skill-installer', 'plugin-creator', 'plugin-management'}
                 else 'general')
        rows.append({'name': name, 'title': title, 'usage': usage, 'description': description,
                     'scope': scope, 'group': group, 'group_label': GROUPS[group],
                     'enabled': len(enabled) == 1,
                     'unavailable_reason': '同名技能有歧义，暂不能手动指定。' if len(enabled) > 1 else '当前不可用。' if not enabled else ''})
    order = list(GROUPS)
    return sorted(rows, key=lambda row: (order.index(row['group']), row['name']))
