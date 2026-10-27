"""Skill labels are presentation only, not activation or path selection."""
import unittest

from shared.skill_catalog import skill_choices
from shared.conversation_catalog import catalog_skills


class SkillCatalogTests(unittest.TestCase):
    def test_chinese_labels_and_groups_preserve_native_names(self):
        rows = skill_choices({'skills': [
            {'name':'daily-report-full-tests','scope':'repo','enabled':True,'path':'/private/project/SKILL.md'},
            {'name':'dingtalk-aitable','scope':'user','enabled':True},
            {'name':'plugin-management:plugin-management','scope':'system','enabled':True},
        ]})
        self.assertEqual(rows[0]['group_label'], '当前项目')
        self.assertEqual(rows[0]['title'], '业务完整测试')
        self.assertEqual(rows[1]['name'], 'dingtalk-aitable')
        self.assertEqual(rows[1]['title'], '钉钉多维表')
        self.assertEqual(rows[1]['group_label'], '钉钉办公')
        self.assertEqual(rows[2]['title'], '插件管理')
        self.assertNotIn('/private', str(rows))

    def test_unknown_descriptions_are_preserved_and_disabled_not_activated(self):
        catalog = {'skills': [
            {'name':'new-skill','description':'这是一个新安装的技能说明。','enabled':True},
            {'name':'disabled-skill','enabled':False},
        ]}
        rows = catalog_skills(catalog)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['usage'], '这是一个新安装的技能说明。')
        self.assertEqual(len(skill_choices(catalog, include_disabled=True)), 2)
        self.assertFalse(next(row for row in skill_choices(catalog, include_disabled=True)
                              if row['name'] == 'disabled-skill')['enabled'])

    def test_duplicate_names_are_not_silently_selected(self):
        catalog = {'skills': [{'name':'duplicate','enabled':True,'scope':'user'},
                              {'name':'duplicate','enabled':True,'scope':'repo'}]}
        rows = catalog_skills(catalog)
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]['enabled'])
        self.assertIn('歧义', rows[0]['unavailable_reason'])
