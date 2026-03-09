import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import server


class TeacherPhraseDeleteTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.bank_path = Path(self.tmpdir.name) / 'teacher_phrase_bank.json'
        self.bank_path.write_text(
            json.dumps(
                {
                    'version': 1,
                    'updated_at': 1,
                    'items': [
                        {
                            'id': 'default_praise_01',
                            'text': '内置表扬',
                            'category': 'praise',
                            'use_count': 0,
                            'created_at': 1,
                            'updated_at': 1,
                            'last_used_at': 0,
                            'builtin': True,
                        },
                        {
                            'id': 'ph_custom_01',
                            'text': '自定义短语',
                            'category': 'advice',
                            'use_count': 2,
                            'created_at': 1,
                            'updated_at': 1,
                            'last_used_at': 1,
                            'builtin': False,
                        },
                    ],
                },
                ensure_ascii=False,
            ),
            encoding='utf-8',
        )
        self.path_patch = patch.object(server, 'TEACHER_PHRASE_BANK_PATH', self.bank_path)
        self.path_patch.start()

    def tearDown(self):
        self.path_patch.stop()
        self.tmpdir.cleanup()

    def test_delete_custom_teacher_phrase_removes_item(self):
        deleted = server._delete_teacher_phrase_from_bank('ph_custom_01')

        bank = json.loads(self.bank_path.read_text(encoding='utf-8'))
        ids = [row['id'] for row in bank['items']]

        self.assertEqual(deleted['id'], 'ph_custom_01')
        self.assertEqual(ids, ['default_praise_01'])

    def test_delete_builtin_teacher_phrase_is_rejected(self):
        with self.assertRaises(server.HTTPException) as ctx:
            server._delete_teacher_phrase_from_bank('default_praise_01')

        bank = json.loads(self.bank_path.read_text(encoding='utf-8'))
        ids = [row['id'] for row in bank['items']]

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(ids, ['default_praise_01', 'ph_custom_01'])


if __name__ == '__main__':
    unittest.main()
