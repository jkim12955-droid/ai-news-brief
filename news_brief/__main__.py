"""python -m news_brief 로 부를 때 들어오는 문이다.

실제 일은 cli 가 한다. 여기서는 종료 코드만 그대로 넘겨준다.
GitHub Actions 가 이 코드로 성공과 실패를 가르기 때문에 삼키면 안 된다.
"""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
