"""V4P3 SP0T Discord 봇 실행 진입점."""

import os

from bot import bot


def main():
    token = os.environ.get('BOT_TOKEN')
    if not token:
        print('BOT_TOKEN이 설정되지 않아 봇을 실행할 수 없습니다.')
        return
    bot.run(token)


if __name__ == '__main__':
    main()
