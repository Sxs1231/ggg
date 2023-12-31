import sqlalchemy as sa
from aiocache import cached

from src.chess_api.get_engine_evaluation import get_engine_evaluation
from src.chess_api.get_limits import get_limits
from src.utils.chess_api.dataclasses import EngineEvaluation
from src.consts import Config
from src.db.db_session import create_session
from src.db.__all_models import User, Settings, Game


async def create_new_user(user_id: int, name: str) -> None:
    """
    Создаёт нового юзера.

    :param user_id: Юзер айди.
    :param name: Имя пользователя
    """

    with create_session(do_commit=True) as db_sess:
        if get_user(user_id):
            return

        user = User(user_id=user_id, name=name)
        db_sess.add(user)

        settings = Settings(user_id=user_id)
        await settings.async_init()
        db_sess.add(settings)


async def create_new_game(user_id: int, orientation: str) -> bool:
    """
    Создаёт новую игру, останавливая, если есть, старую.

    :param user_id: Юзер айди.
    :param orientation: Цвет игрока.
    :return: True, если была прекращена старая игра.
    """

    if orientation not in ('w', 'b'):
        raise ValueError('Цвет игрока должен быть "w" или "b"')

    is_was_old_game = isinstance(
        await stop_current_game(user_id, is_resign=True), tuple
    )

    with create_session(do_commit=True) as db_sess:
        game = Game(
            user_id=user_id,
            orientation=orientation,
            is_active=True
        )
        db_sess.add(game)
        db_sess.commit()

    return is_was_old_game


def get_user(user_id: int) -> User | None:
    """
    Возвращает пользователя по айдишнику.

    :param user_id: Юзер айди.
    :return: Модель Юзер.
    """

    with create_session() as db_sess:
        query = sa.select(User).where(User.user_id == user_id)
        return db_sess.scalar(query)


def get_settings(user_id: int) -> Settings:
    """
    Возвращает настройки движка по айдишнику.

    :param user_id: Юзер айди.
    :return: Модель Настройки.
    """

    with create_session() as db_sess:
        query = sa.select(Settings).where(Settings.user_id == user_id)
        return db_sess.scalar(query)


def get_current_game(user_id: int) -> Game:
    """
    Возвращает текущую игру по айдишнику.

    :param user_id: Юзер айди.
    :return: Модель Игры.
    """

    with create_session() as db_sess:
        query = sa.select(Game).where(
            Game.user_id == user_id,
            Game.is_active == True  # noqa
        )
        game = db_sess.scalar(query)
        return game


async def stop_current_game(
        user_id: int,
        is_resign: bool = False
) -> tuple[EngineEvaluation, str | None] | None:
    """
    Останавливает текущую игру и возвращает оценку позиции от движка.

    :param user_id: Юзер айди.
    :param is_resign: Сдаётся ли пользователь.
    :return: dict если игра есть, None если не юзер не играет.
    """

    game = get_current_game(user_id)
    if not game:
        return None

    evaluation = await get_engine_evaluation(fen=game.fen)
    if is_resign:
        who_win = 'w' if game.orientation == 'b' else 'b'
    elif evaluation.is_end:
        who_win = evaluation.who_win
    elif evaluation.end_type == "checkmate":
        who_win = 'w' if evaluation.value > 0 else 'b'
    else:
        who_win = None

    with create_session(do_commit=True) as db_sess:
        query = sa.update(Game).where(
            Game.user_id == user_id,
            Game.is_active == True  # noqa
        ).values(
            is_active=False,
            who_win=who_win
        )
        db_sess.execute(query)

        query = sa.select(User).where(User.user_id == user_id)
        user = db_sess.scalar(query)

        query = sa.update(User).where(
            User.user_id == user_id
        ).values(
            total_games=user.total_games + 1,
            total_wins=(
                user.total_wins + 1
                if game.orientation == who_win
                else user.total_wins
            ),
            total_defeats=(
                user.total_defeats + 1
                if who_win is not None and game.orientation != who_win
                else user.total_defeats
            ),
            total_draws=(
                user.total_draws + 1
                if who_win is None
                else user.total_draws
            )
        )
        db_sess.execute(query)

    return evaluation, who_win


def update_current_game(
        user_id: int,
        *,
        prev_moves: str,
        last_move: str,
        check: str | None,
        fen: str,
) -> bool | None:
    """
    Обновляет текущую игру.

    :param user_id: Юзер айди.
    :param prev_moves: Новая история ходов.
    :param last_move: Последний ход.
    :param check: Клетка с шахом.
    :param fen: FEN позиция.
    :return: True если обновил, False если закончилась, None если игры нет.
    """

    if not get_current_game(user_id):
        return None

    with create_session(do_commit=True) as db_sess:
        query = sa.update(Game).where(
            Game.user_id == user_id,
            Game.is_active == True  # noqa
        ).values(
            prev_moves=prev_moves,
            fen=fen,
            last_move=last_move,
            check=check,
        )
        db_sess.execute(query)

    return True


async def update_settings(user_id: int, **params) -> dict:
    """
    Обновляет настройки движка по юзер айди.

    :param user_id: Юзер айди.
    :param params: Настройки.
    :return: Новые значения.
    """

    limits = await get_limits()
    for param in params.keys():
        try:
            params[param] = max(
                min(
                    params[param], limits[param]["max"]
                ),
                limits[param]["min"]
            )
        except KeyError:
            pass

    with create_session(do_commit=True) as db_sess:
        query = sa.update(Settings).where(
            Settings.user_id == user_id
        ).values(
            **params
        )
        db_sess.execute(query)

    return params


@cached(ttl=Config.CACHE_GLOBAL_TOP)
async def get_global_statistic() -> str:
    """
    Возвращает топ Config.GLOBAL_TOP игроков в отформатированном виде.
    """

    with create_session() as db_sess:
        query = sa.select(User).order_by(
            sa.desc(User.total_wins),
            User.created_time,
        ).limit(
            Config.GLOBAL_TOP
        )

        users = db_sess.scalars(query)

        line = "[{0}](tg://user?id={1}) - {2}/{3}/{4}/{5} - {6}%"
        message = '\n'.join(
            [
                '*Ник - W/D/L/Total - WinRate*',
                *(
                    line.format(
                        user.name, user.user_id, user.total_wins,
                        user.total_draws, user.total_defeats, user.total_games,
                        round(user.total_wins / (user.total_games or 1) * 100)
                    )
                    for user in users
                ),
                '\nСтатистика обновляется раз в '
                f'*{Config.CACHE_GLOBAL_TOP} секунд*'
            ]
        )

    return message
