import sys
import os
import unittest

# 将 CS2Toolkit 目录添加到 sys.path，以便可以正确导入 app 模块
project_root = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, project_root)

if __name__ == '__main__':
    # 发现并运行 tests 目录下的所有测试
    loader = unittest.TestLoader()
    suite = loader.discover(os.path.join(project_root, 'tests'))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
