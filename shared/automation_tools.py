"""Web-owned scheduled-task tool, registered only on newly created threads."""
import json

TOOL_NAME = 'manage_scheduled_task'
AUTOMATION_CONTEXT = '''工作台已提供 manage_scheduled_task 工具。部署是否启用以工具回执为准；若提示部署未启用，需要实例所有者在电脑面板或安装配置启用，不是对话权限不足，不要绕过配置另建调度。只有用户明确要求未来或重复执行时，才创建/修改定时任务；普通讨论、检查、进度说明不能创建。工具管理本对话的任务，成功回执前不能声称已创建。时间默认 Asia/Hong_Kong，支持 interval（interval_minutes整数分钟）、daily（time HH:MM、timezone）、weekly（time、weekdays周一0至周日6、timezone）、once（run_at带时区ISO时间）。用户在对话中明确要求创建或恢复即授权按本对话既有权限定时执行，所有权限模式均可通过工具直接启用，无需转到管理页再次确认；不得扩大用户授权范围。用户可在对话中要求修改、暂停、恢复或删除，管理页只是可选入口。创建需 title、每轮实际执行的 prompt、schedule；prompt 要保存可独立执行的授权范围、输出要求及必要路径，不要把“创建定时任务”指令再次当作每轮内容。更新、暂停、恢复、删除前先 list/get 取得 task_id 与 revision。任务绑定原对话，网页关闭仍执行，模型/技能沿用该对话设置；每轮结果与失败记录持久保存。只在成功回执后告知规则、下一次执行时间、沿用的权限及如何暂停。权限或工作目录变化会阻止执行，需明确重新创建；不确定投递不自动重发。不要用 shell、cron 或文件绕过此管理工具。'''


def scheduled_task_dynamic_tool():
    return {
        'type': 'function',
        'name': TOOL_NAME,
        'description': '在用户明确要求时管理本对话的持久化定时任务。后台每次启动同一对话的新一轮；不能管理其他对话。支持 create/list/get/runs/update/pause/resume/delete。更新动作需最新 revision；本工具回执 success=true 才能报告成功。定时运行本身不能创建或修改其他定时任务。',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'action': {'type': 'string', 'enum': ['create','list','get','runs','update','pause','resume','delete']},
                'task_id': {'type': 'string'},
                'revision': {'type': 'integer', 'minimum': 1},
                'title': {'type': 'string', 'maxLength': 120},
                'prompt': {'type': 'string', 'maxLength': 60000},
                'schedule': {'type': 'object', 'properties': {
                    'kind': {'type':'string','enum':['interval','daily','weekly','once']},
                    'interval_minutes': {'type':'integer','minimum':1,'maximum':525600},
                    'time': {'type':'string','description':'HH:MM'},
                    'timezone': {'type':'string'},
                    'weekdays': {'type':'array','items':{'type':'integer','minimum':0,'maximum':6}},
                    'run_at': {'type':'string','description':'ISO 8601 including timezone'},
                }, 'required':['kind'], 'additionalProperties':False},
            },
            'required':['action'], 'additionalProperties':False,
        },
    }


def tool_response(value):
    return {'success': True, 'contentItems': [{'type':'inputText','text':json.dumps(value,ensure_ascii=False)}]}
