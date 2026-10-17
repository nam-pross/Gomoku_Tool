"""
Đọc bàn cờ caro trên màn hình và gợi ý một nước.

Mở game bằng trình duyệt, rồi kéo một khung chứa cả bàn cờ.
Thừa viền trắng cũng được. Tool tìm các đường kẻ, đọc quân đen và quân trắng,
rồi khoanh nước nên đi. Phím F2 bật hoặc tắt tự click. Tự động tự nhận mình đi trước hay đi sau,
chỉ đánh khi đến lượt mình, và đứng im trong lúc đối thủ suy nghĩ.

    python app.py
    python app.py --side first
    python app.py --side second

Trước là người đi nước đầu, Sau là người kia. Màu của hai bên
lấy từ số quân trên bàn, không gắn cứng X với Trước.
"""

from __future__ import annotations

import argparse
import ctypes
import threading
import time
import tkinter as tk
from functools import lru_cache

from PIL import ImageGrab

EMPTY = 0
BLACK = 1
WHITE = 2

# Threats are five, open four, closed four, open three, and a fork: two
# threats whose block squares are different, so one reply cannot stop both.
# A double open three (two .XXX. lines through one move) is a winning fork
# when each three can still grow into an open four. The position score counts
# those shapes for both colors. Search is negamax.
# Fives, forks and fours stay in the move list; only quiet moves are capped.

_INF = 10**15
_MATE = 10**12

_AXES = ((1, 0), (0, 1), (1, 1), (1, -1))
_RAYS = ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (-1, -1), (1, -1), (-1, 1))
_SWAP = str.maketrans("12", "21")


def opponent(player: int) -> int:
    return WHITE if player == BLACK else BLACK


def whose_turn(board: list[int], remembered: int = BLACK) -> tuple[int | None, int, int, int]:
    """Return the side to move, the opener, and the two stone counts.

    One pass. The side with fewer stones moves, so a lead of one says who
    opened. White can open: then white is ahead and it is black's turn.
    Equal counts are the opener's turn again. The remembered opener is only
    used while the counts are level. A wider gap is a bad reading: the turn
    is unknown and the remembered opener is left as it was.
    """
    black = 0
    white = 0
    for value in board:
        if value == BLACK:
            black += 1
        elif value == WHITE:
            white += 1
    opener = remembered if remembered in (BLACK, WHITE) else BLACK
    if black == 0 and white == 0:
        return BLACK, BLACK, 0, 0
    gap = black - white
    if gap == 0:
        return opener, opener, black, white
    if gap == 1:
        return WHITE, BLACK, black, white
    if gap == -1:
        return BLACK, WHITE, black, white
    return None, opener, black, white


def _seat(player: int, opener: int) -> str:
    """'đi trước (đen)' or 'đi sau (trắng)'."""
    role = "trước" if player == opener else "sau"
    color = "đen" if player == BLACK else "trắng"
    return f"đi {role} ({color})"


# Empty board: wait before deciding we move first. Occupied board: wait to
# see whether the opponent is still about to play. A failed guess waits
# this long before one more try.
_OPEN_WAIT = 2.0
_LOOK_WAIT = 1.1
_RETRY_GAP = 4.0


class _Seat:
    """The color auto-play has claimed, and how sure that claim is."""

    def __init__(self) -> None:
        self.color = None
        self.assumed = False
        self.confirmed = False
        self.waiting = False
        self.allow = False
        self.watch = None
        self.armed = 0.0
        self.retry_at = 0.0
        self.pending = None

    def reset(self, now: float) -> None:
        self.color = None
        self.assumed = False
        self.confirmed = False
        self.waiting = False
        self.allow = False
        self.watch = None
        self.armed = now
        self.retry_at = 0.0
        self.pending = None


def resolve_seat(seat: _Seat, board, turn, opener: int, settled: bool, now: float):
    """Return our color, or None while auto is still watching the table.

    A stone that appears without us playing it belongs to the opponent, so
    we take the other color. A quiet empty board, after a short wait, means
    we move first. A quiet occupied board means the side to move is us.
    Both of those are guesses until a click of ours actually leaves a stone.
    """
    if seat.watch is not None and len(seat.watch) != len(board):
        seat.reset(now)
    if settled and seat.watch and any(seat.watch) and not any(board):
        seat.reset(now)
        return None, "Bàn mới. Đang xem mình đi trước hay sau."
    if seat.confirmed and seat.color in (BLACK, WHITE):
        return seat.color, ""
    if not settled or turn is None:
        return None, "Đang xem mình đi trước hay sau."
    if seat.watch is None:
        seat.watch = tuple(board)
        seat.armed = now
        return None, "Đang xem mình đi trước hay sau."

    appeared = []
    for index, value in enumerate(board):
        old = seat.watch[index]
        if value and old != value:
            appeared.append(value)
    if appeared:
        colors = set(appeared)
        if len(colors) == 1:
            seat.color = opponent(next(iter(colors)))
            seat.confirmed = True
            seat.assumed = False
            seat.waiting = False
            seat.allow = False
            seat.watch = tuple(board)
            return seat.color, ""
        seat.watch = tuple(board)
        seat.armed = now
        seat.color = None
        seat.assumed = False
        return None, "Đang xem mình đi trước hay sau."

    if seat.assumed and seat.color in (BLACK, WHITE):
        return seat.color, ""
    if seat.waiting and not seat.allow:
        if now < seat.retry_at:
            return None, "Đang chờ đối thủ suy nghĩ."
        seat.waiting = False
        seat.allow = True
    if not seat.allow:
        if not any(board):
            if now - seat.armed < _OPEN_WAIT:
                return None, "Bàn trống. Đang chờ xem đối thủ có đi trước không."
        elif now - seat.armed < _LOOK_WAIT:
            return None, "Đang chờ xem đối thủ có đang suy nghĩ không."
    seat.allow = False
    seat.color = opener if not any(board) else turn
    seat.assumed = True
    seat.watch = tuple(board)
    return seat.color, ""


def winner(board: list[int], size: int) -> int:
    """The player who already has five or more in a row, or 0."""
    for y in range(size):
        for x in range(size):
            player = board[y * size + x]
            if not player:
                continue
            for dx, dy in _AXES:
                px, py = x - dx, y - dy
                if _inside(size, px, py) and board[py * size + px] == player:
                    continue
                count = 0
                nx, ny = x, y
                while _inside(size, nx, ny) and board[ny * size + nx] == player:
                    count += 1
                    nx += dx
                    ny += dy
                if count >= 5:
                    return player
    return 0


def choose_move(
    board: list[int],
    size: int,
    player: int,
    depth: int = 6,
    time_limit: float | None = 1.0,
    complete: bool = False,
    live=None,
) -> tuple[int, int] | None:
    """Return the column and row to play, or None when the game is over or full.

    Take a five, else the square that blocks theirs. An open four or two
    separate fours is taken next, because one block cannot answer it. Anything
    else is alpha-beta. complete does not stop when two depths agree. The first
    depth always finishes.
    """
    if len(board) != size * size:
        raise ValueError("board length does not match size")
    if player not in (BLACK, WHITE):
        raise ValueError("player must be black or white")
    if winner(board, size) or not any(value == EMPTY for value in board):
        return None
    if not any(board):
        mid = size // 2
        return mid, mid

    # Only an immediate five, the one square that stops theirs, or a threat
    # that still wins after their best reply. Scan order is not a reason.
    forced = _best_immediate(board, size, player)
    if forced is not None:
        return forced

    pos = _Board(board, size)
    ranked = pos._ranked(player)
    if not ranked:
        return None
    deadline = None if not time_limit else time.perf_counter() + time_limit
    depth = max(1, min(8, depth))
    best = None
    prev = None
    for level in range(1, depth + 1):
        if live is not None and not live():
            break
        if deadline is not None and level > 1 and time.perf_counter() >= deadline:
            break
        try:
            # The first pass always finishes. Later passes stop at the deadline
            # so a deeper request still returns the best finished depth.
            score, move = _search(
                pos, player, level, -_INF, _INF, None if level == 1 else deadline, live, True
            )
        except _Timeout:
            break
        if move is None:
            break
        best = move
        # A proven five, or two winning squares, ends the search. A short
        # search may also stop once two deeper passes agree.
        if abs(score) >= _MATE - 2 or (not complete and move == prev and level >= 3):
            break
        prev = move
    if best is None:
        points = pos.candidates(player)
        if not points:
            empties = [index for index, value in enumerate(board) if value == EMPTY]
            if not empties:
                return None
            center = size // 2
            best = min(empties, key=lambda index: abs(index % size - center) + abs(index // size - center))
        else:
            center = size // 2
            best = min(points, key=lambda index: abs(index % size - center) + abs(index // size - center))
    x, y = best % size, best // size
    return _guard_move(board, size, player, (x, y))


def _inside(size: int, x: int, y: int) -> bool:
    return 0 <= x < size and 0 <= y < size


def _window_score(text: str) -> tuple[int, int]:
    """Threat score of one line, and how many long open threes it holds.

    1 is the side being counted, 2 is a wall. Each shape is counted once.
    An open four is not also a pair of closed fours, and a plain run of two
    stones stays cheap. A long open three has two empty cells on one side,
    so it can grow into an open four.
    """
    score = 0
    longs = 0
    start = 0
    length = len(text)
    while start < length:
        if text[start] == "2":
            start += 1
            continue
        end = start
        while end < length and text[end] != "2":
            end += 1
        part = text[start:end]
        score += _segment_score(part)
        longs += _long_open_threes(part)
        start = end
    return score, longs


def _long_open_threes(seg: str) -> int:
    """Open threes with a spare empty cell, so one extension makes an open four."""
    count = 0
    index = 0
    limit = len(seg) - 6
    while index <= limit:
        if (
            seg.startswith("001110", index)
            or seg.startswith("011100", index)
            or seg.startswith("011010", index)
            or seg.startswith("010110", index)
        ):
            count += 1
            index += 6
        else:
            index += 1
    return count


def _segment_score(seg: str) -> int:
    """Score one stretch with no opponent stones in it."""
    if len(seg) < 5 or "1" not in seg:
        return 0
    used = [False] * len(seg)
    score = 0

    def take(pattern: str, points: int) -> None:
        nonlocal score
        at = 0
        size = len(pattern)
        while True:
            found = seg.find(pattern, at)
            if found < 0:
                return
            if any(used[found : found + size]):
                at = found + 1
                continue
            for index in range(found, found + size):
                used[index] = True
            score += points
            at = found + size

    take("11111", 100_000_000)
    take("011110", 1_000_000)
    for pattern in ("11110", "01111", "11011", "11101", "10111"):
        take(pattern, 50_000)
    take("01110", 8_000)
    take("011010", 2_000)
    take("010110", 2_000)
    take("001110", 700)
    take("011100", 700)
    return score


def _line_scores(text: str) -> tuple[int, int, int, int]:
    """Black score, white score, and each side's long open threes on one line."""
    black, black_long = _window_score(text)
    white, white_long = _window_score(text.translate(_SWAP))
    return black, white, black_long, white_long


def _score_cells(board: list[int], cells: tuple[int, ...]) -> tuple[int, int, int, int]:
    return _line_scores("".join(str(board[index]) for index in cells))


@lru_cache(maxsize=8)
def _geometry(size: int):
    """Lines of length at least 5, the lines through each cell, and ray neighbors."""
    lines: list[tuple[int, ...]] = []
    through: list[list[int]] = [[] for _index in range(size * size)]

    def add(cells: list[int]) -> None:
        if len(cells) < 5:
            return
        number = len(lines)
        lines.append(tuple(cells))
        for index in cells:
            through[index].append(number)

    for dx, dy in _AXES:
        for y in range(size):
            for x in range(size):
                if _inside(size, x - dx, y - dy):
                    continue
                cells = []
                nx, ny = x, y
                while _inside(size, nx, ny):
                    cells.append(ny * size + nx)
                    nx += dx
                    ny += dy
                add(cells)

    neigh = []
    for y in range(size):
        for x in range(size):
            near = []
            for dx, dy in _RAYS:
                for step in (1, 2):
                    nx = x + dx * step
                    ny = y + dy * step
                    if not _inside(size, nx, ny):
                        break
                    near.append(ny * size + nx)
            neigh.append(tuple(near))
    return tuple(lines), tuple(tuple(item) for item in through), tuple(neigh)


def _makes_five(board: list[int], size: int, x: int, y: int, player: int) -> bool:
    """True when playing here completes five or more in a row."""
    for dx, dy in _AXES:
        count = 1
        for sign in (1, -1):
            nx = x + sign * dx
            ny = y + sign * dy
            for _step in range(4):
                if not _inside(size, nx, ny) or board[ny * size + nx] != player:
                    break
                count += 1
                nx += sign * dx
                ny += sign * dy
        if count >= 5:
            return True
    return False


def _broken_live_axis(text: str, coords: list[int]) -> tuple[list[int], list[int]] | None:
    """A gap-three on this window: both wings empty, the gap makes an open four.

    Returns the squares the opponent can answer, and the gap to play next.
    The center stone of the window has to be one of the three.
    """
    center = 4
    for pattern, gaps in (("011010", (3,)), ("010110", (2,))):
        length = len(pattern)
        for start in range(9 - length + 1):
            if not (start <= center < start + length):
                continue
            if text[start : start + length] != pattern or pattern[center - start] != "1":
                continue
            extends = []
            for offset in gaps:
                point = coords[start + offset]
                if point < 0:
                    extends = []
                    break
                extends.append(point)
            if not extends:
                continue
            blocks = list(extends)
            for offset in (0, length - 1):
                point = coords[start + offset]
                if point >= 0 and point not in blocks:
                    blocks.append(point)
            return blocks, extends
    return None


def _analyze_move(board: list[int], size: int, x: int, y: int, player: int):
    """Shapes made by playing at this empty cell.

    Returns fives, open fours, closed fours, open threes, half-open threes,
    the squares that would then finish a five, and the ends of open threes.
    """
    fives = open4 = closed4 = open3 = half3 = open2 = 0
    wins = []
    ends = []
    for dx, dy in _AXES:
        chars = []
        coords = []
        for step in range(-4, 5):
            nx = x + dx * step
            ny = y + dy * step
            if step == 0:
                chars.append("1")
                coords.append(y * size + x)
                continue
            if not _inside(size, nx, ny):
                chars.append("2")
                coords.append(-1)
                continue
            value = board[ny * size + nx]
            if value == EMPTY:
                chars.append("0")
            elif value == player:
                chars.append("1")
            else:
                chars.append("2")
            coords.append(ny * size + nx)
        text = "".join(chars)
        center = 4
        dir_wins = []
        five = False
        for start in range(5):
            window = text[start : start + 5]
            if window == "11111":
                five = True
                break
            if window.count("1") == 4 and window.count("0") == 1:
                point = coords[start + window.index("0")]
                if point >= 0 and point not in dir_wins:
                    dir_wins.append(point)
        left = center
        while left > 0 and text[left - 1] == "1":
            left -= 1
        right = center
        while right + 1 < 9 and text[right + 1] == "1":
            right += 1
        length = right - left + 1
        left_open = left > 0 and text[left - 1] == "0"
        right_open = right + 1 < 9 and text[right + 1] == "0"
        if five or length >= 5:
            fives += 1
            continue
        if length == 4 and left_open and right_open:
            open4 += 1
            for point in dir_wins:
                if point not in wins:
                    wins.append(point)
            continue
        if dir_wins:
            closed4 += 1
            for point in dir_wins:
                if point not in wins:
                    wins.append(point)
            continue
        if length == 3 and left_open and right_open:
            open3 += 1
            for slot in (left - 1, right + 1):
                point = coords[slot]
                if point >= 0 and point not in ends:
                    ends.append(point)
        elif _broken_live_axis(text, coords) is not None:
            # XX.X or X.XX, both wings empty. One move in the gap makes an open four.
            open3 += 1
        elif length == 3 and (left_open or right_open):
            half3 += 1
        elif length == 2 and left_open and right_open:
            open2 += 1
    return fives, open4, closed4, open3, half3, wins, ends, open2


def _forcing(shape) -> bool:
    """True when one reply cannot answer the threats this move creates."""
    fives, open4, _closed4, open3, _half3, wins, _ends = shape[:7]
    if fives or open4 or len(wins) >= 2:
        return True
    # A four plus a separate open three: blocking the four leaves the three.
    return bool(wins) and open3 > 0 and any(point not in _ends for point in wins)


def _would_open_threes(board: list[int], size: int, x: int, y: int, player: int) -> int:
    """How many open threes a stone at this empty cell would make."""
    if not _inside(size, x, y) or board[y * size + x] != EMPTY:
        return 0
    count = 0
    for dx, dy in _AXES:
        left = 0
        nx, ny = x - dx, y - dy
        while left < 4 and _inside(size, nx, ny) and board[ny * size + nx] == player:
            left += 1
            nx -= dx
            ny -= dy
        right = 0
        nx, ny = x + dx, y + dy
        while right < 4 and _inside(size, nx, ny) and board[ny * size + nx] == player:
            right += 1
            nx += dx
            ny += dy
        if left + right + 1 != 3:
            continue
        lx, ly = x - dx * (left + 1), y - dy * (left + 1)
        rx, ry = x + dx * (right + 1), y + dy * (right + 1)
        if (
            _inside(size, lx, ly)
            and board[ly * size + lx] == EMPTY
            and _inside(size, rx, ry)
            and board[ry * size + rx] == EMPTY
        ):
            count += 1
    return count


def _prepares_double_three(board: list[int], size: int, x: int, y: int, player: int) -> bool:
    """True when this move leaves an empty cell that is then a double three."""
    cell = y * size + x
    board[cell] = player
    try:
        for dx, dy in _AXES:
            for sign in (1, -1):
                mx, my = x + sign * dx, y + sign * dy
                if not _inside(size, mx, my) or board[my * size + mx] != player:
                    continue
                ex, ey = x + 2 * sign * dx, y + 2 * sign * dy
                if not _inside(size, ex, ey) or board[ey * size + ex] != EMPTY:
                    continue
                if _would_open_threes(board, size, ex, ey, player) >= 2:
                    return True
        for dx, dy in _RAYS:
            ex, ey = x + dx, y + dy
            if not _inside(size, ex, ey) or board[ey * size + ex] != EMPTY:
                continue
            if _would_open_threes(board, size, ex, ey, player) >= 2:
                return True
        return False
    finally:
        board[cell] = EMPTY


def _live_threes(board: list[int], size: int, x: int, y: int, player: int) -> list[tuple[list[int], list[int]]]:
    """Open threes through a stone. Each is (block ends, ends that make an open four)."""
    found = []
    for dx, dy in _AXES:
        left = 0
        nx, ny = x - dx, y - dy
        while left < 4 and _inside(size, nx, ny) and board[ny * size + nx] == player:
            left += 1
            nx -= dx
            ny -= dy
        right = 0
        nx, ny = x + dx, y + dy
        while right < 4 and _inside(size, nx, ny) and board[ny * size + nx] == player:
            right += 1
            nx += dx
            ny += dy
        if left + right + 1 != 3:
            chars = []
            coords = []
            for step in range(-4, 5):
                sx = x + dx * step
                sy = y + dy * step
                if step == 0:
                    chars.append("1")
                    coords.append(y * size + x)
                    continue
                if not _inside(size, sx, sy):
                    chars.append("2")
                    coords.append(-1)
                    continue
                value = board[sy * size + sx]
                if value == EMPTY:
                    chars.append("0")
                elif value == player:
                    chars.append("1")
                else:
                    chars.append("2")
                coords.append(sy * size + sx)
            broken = _broken_live_axis("".join(chars), coords)
            if broken is not None:
                found.append(broken)
            continue
        lx, ly = x - dx * (left + 1), y - dy * (left + 1)
        rx, ry = x + dx * (right + 1), y + dy * (right + 1)
        if not (_inside(size, lx, ly) and board[ly * size + lx] == EMPTY):
            continue
        if not (_inside(size, rx, ry) and board[ry * size + rx] == EMPTY):
            continue
        ends = [ly * size + lx, ry * size + rx]
        extends = []
        bx, by = lx - dx, ly - dy
        if _inside(size, bx, by) and board[by * size + bx] == EMPTY:
            extends.append(ends[0])
        bx, by = rx + dx, ry + dy
        if _inside(size, bx, by) and board[by * size + bx] == EMPTY:
            extends.append(ends[1])
        if extends:
            found.append((ends, extends))
    return found


def _all_live_threes(board: list[int], size: int, player: int) -> list[tuple[list[int], list[int]]]:
    """Every open three on the board, straight or with one gap, counted once."""
    seen = set()
    found = []
    for index, value in enumerate(board):
        if value != player:
            continue
        for ends, extends in _live_threes(board, size, index % size, index // size, player):
            key = tuple(sorted(ends))
            if key in seen:
                continue
            seen.add(key)
            found.append((ends, extends))
    return found


def _any_three_converts(board: list[int], size: int, player: int) -> bool:
    """Some remaining three grows into an open four one block cannot cover."""
    seen = set()
    for _ends, extends in _all_live_threes(board, size, player):
        for ext in extends:
            if ext in seen or board[ext] != EMPTY:
                continue
            seen.add(ext)
            board[ext] = player
            try:
                if winner(board, size) == player or len(_win_squares(board, size, player)) >= 2:
                    return True
            finally:
                board[ext] = EMPTY
    return False


def _three_three_wins(board: list[int], size: int, x: int, y: int, player: int) -> bool:
    """True when two open threes here still win after every single block.

    The opponent can stop only one three. The other must grow into an open
    four, and that four has two winning squares, so the next block fails too.
    """
    cell = y * size + x
    if board[cell] != EMPTY:
        return False
    board[cell] = player
    try:
        if _win_squares(board, size, opponent(player)) or _urgent_cells(board, size, opponent(player)):
            return False
        # The move has to join a three. A second three may already sit elsewhere.
        if not _live_threes(board, size, x, y, player):
            return False
        threes = _all_live_threes(board, size, player)
        if len(threes) < 2:
            return False
        blocks = []
        for ends, _extends in threes:
            for end in ends:
                if end not in blocks:
                    blocks.append(end)
        other = opponent(player)
        for block in blocks:
            board[block] = other
            try:
                if _win_squares(board, size, other):
                    return False
                if not _any_three_converts(board, size, player):
                    return False
            finally:
                board[block] = EMPTY
        return True
    finally:
        board[cell] = EMPTY


def _nearby(board: list[int], size: int) -> list[int]:
    """Empty cells within two steps of a stone."""
    _lines, _through, neigh = _geometry(size)
    seen = [False] * (size * size)
    found = []
    for index, value in enumerate(board):
        if not value:
            continue
        for cell in neigh[index]:
            if board[cell] or seen[cell]:
                continue
            seen[cell] = True
            found.append(cell)
    return found


def _move_value(board: list[int], size: int, index: int, player: int) -> int:
    """Classified value of one empty cell, for us and as a block.

    The order is a starting point, not an absolute rule. Blocking their win
    outranks our open four, because that four is useless if they win next.
    A forcing double threat outranks a single open four. Broken threes and
    quiet development stay below both.
    """
    return _classify(board, size, index, player)[1]


def _classify(
    board: list[int],
    size: int,
    index: int,
    player: int,
    my_long: int = 0,
    their_long: int = 0,
) -> tuple[int, int]:
    """Return a tier and a score. Higher tier is never trimmed away first."""
    x = index % size
    y = index // size
    mine = _analyze_move(board, size, x, y, player)
    theirs = _analyze_move(board, size, x, y, opponent(player))
    mf, mo4, mc4, mo3, mh, mwins, _me, mo2 = mine
    tf, _to4, tc4, to3, th, twins, _te, to2 = theirs
    if mf:
        return 5, 5_000_000 + len(mwins) * 100 + mo4 * 10 + mo3
    if tf:
        return 4, 4_000_000 + len(twins) * 100 + mc4
    if _forcing(mine):
        return 3, 900_000 + len(mwins) * 1_000 + mo4 * 500 + mo3 * 20
    if _forcing(theirs):
        return 3, 860_000 + len(twins) * 1_000 + tc4 * 20
    # Two open threes. One reply cannot block both lines.
    if mo3 >= 2 or (mo3 >= 1 and my_long >= 1):
        return 3, 820_000 + mo3 * 50 + mo2
    if to3 >= 2 or (to3 >= 1 and their_long >= 1):
        return 3, 780_000 + to3 * 50
    # A three plus a second line, or the stone that makes a double three next.
    if (mo3 and mo2) or (mo2 and _prepares_double_three(board, size, x, y, player)):
        return 3, 640_000 + mo3 * 20 + mo2
    if mc4:
        return 2, 90_000 + mc4 * 10 + mo3
    if tc4:
        return 2, 85_000 + tc4 * 10
    if mo2 >= 2:
        return 2, 55_000 + mo2 * 10
    if mo3:
        return 1, 16_000
    if to3:
        return 1, 15_000
    if mh or th or to2:
        return 1, 2_000 + mh * 10 + th * 5
    return 0, 0


def _urgent_cells(board: list[int], size: int, player: int) -> list[int]:
    """Empty cells that make two winning squares. One reply cannot cover both."""
    found = []
    for cell in _nearby(board, size):
        shape = _analyze_move(board, size, cell % size, cell // size, player)
        if len(shape[5]) >= 2:
            found.append(cell)
    return found


def _urgent_answers(board: list[int], size: int, player: int) -> tuple[list[int], list[int]]:
    """Squares that answer every open-four threat, then squares that answer fewer.

    Both ends of one three can answer it. Two separate threes need the shared
    square; blocking only one end leaves the other three alive.
    """
    other = opponent(player)
    urgent = _urgent_cells(board, size, other)
    if not urgent:
        return [], []
    saves = []
    partial = []
    for cell in urgent:
        board[cell] = player
        try:
            left = len(_urgent_cells(board, size, other))
        finally:
            board[cell] = EMPTY
        if left == 0:
            saves.append(cell)
        else:
            partial.append(cell)
    return saves, partial


def _attack_key(board: list[int], size: int, player: int, cell: int) -> tuple:
    """Richer threats first. A nearer center breaks the remaining ties."""
    center = size // 2
    x = cell % size
    y = cell // size
    five, open4, closed4, open3, half3, wins, _ends, open2 = _analyze_move(board, size, x, y, player)
    return (five, len(wins), open4, closed4, open3, open2, half3, -(abs(x - center) + abs(y - center)))


def _prefer_attack(board: list[int], size: int, player: int, cells: list[int]) -> int:
    """The block or threat that also builds our own line."""
    return max(cells, key=lambda cell: _attack_key(board, size, player, cell))


def _win_squares(board: list[int], size: int, player: int) -> list[int]:
    """Empty cells where player would complete five."""
    found = []
    for cell in _nearby(board, size):
        if _analyze_move(board, size, cell % size, cell // size, player)[0]:
            found.append(cell)
    return found


def _has_forcing_attack(board: list[int], size: int, player: int) -> bool:
    """True when player can make an open four or two winning squares."""
    for cell in _nearby(board, size):
        shape = _analyze_move(board, size, cell % size, cell // size, player)
        if shape[0] or shape[1] or len(shape[5]) >= 2:
            return True
    return False


def _reply_holds(board: list[int], size: int, cell: int, player: int) -> bool:
    """True when this move still wins after the opponent's best reply.

    A block that only delays a second threat does not count. Two winning
    squares cannot both be answered. One winning square is answered, then
    the board is checked again.
    """
    board[cell] = player
    try:
        if winner(board, size) == player:
            return True
        other = opponent(player)
        if _win_squares(board, size, other):
            return False
        ours = _win_squares(board, size, player)
        if len(ours) >= 2:
            return True
        if len(ours) != 1:
            return False
        # They will not block a single four if they can make two winning
        # squares of their own instead.
        block = ours[0]
        if any(cell != block for cell in _urgent_cells(board, size, other)):
            return False
        board[block] = other
        try:
            if _win_squares(board, size, player) or _urgent_cells(board, size, player):
                return True
        finally:
            board[block] = EMPTY
        return False
    finally:
        board[cell] = EMPTY


def _setup_keeps_eye(board: list[int], size: int, x: int, y: int, player: int) -> bool:
    """This open three leaves a double-three square that one block cannot take."""
    cell = y * size + x
    board[cell] = player
    try:
        eyes = []
        for dx, dy in _AXES:
            for sign in (1, -1):
                for dist in (1, 2):
                    ex = x + sign * dx * dist
                    ey = y + sign * dy * dist
                    if not _inside(size, ex, ey) or board[ey * size + ex] != EMPTY:
                        continue
                    if _would_open_threes(board, size, ex, ey, player) < 2:
                        continue
                    if _three_three_wins(board, size, ex, ey, player):
                        eyes.append(ey * size + ex)
        if not eyes:
            return False
        blocks = []
        for ends, _extends in _live_threes(board, size, x, y, player):
            for end in ends:
                if end not in blocks:
                    blocks.append(end)
        if not blocks:
            return False
        other = opponent(player)
        for block in blocks:
            board[block] = other
            try:
                if not any(
                    board[eye] == EMPTY
                    and _three_three_wins(board, size, eye % size, eye // size, player)
                    for eye in eyes
                ):
                    return False
            finally:
                board[block] = EMPTY
        return True
    finally:
        board[cell] = EMPTY


def _best_forcing_setup(board: list[int], size: int, player: int) -> tuple[int, int] | None:
    """An open three that forces the reply, then the next move is a winning double three."""
    best = None
    best_key = None
    center = size // 2
    for cell in _nearby(board, size):
        x, y = cell % size, cell // size
        shape = _analyze_move(board, size, x, y, player)
        if shape[3] < 1 or shape[7] < 1:
            continue
        if not _setup_keeps_eye(board, size, x, y, player):
            continue
        key = (shape[3], shape[7], -(abs(x - center) + abs(y - center)))
        if best_key is None or key > best_key:
            best = (x, y)
            best_key = key
    return best


def _best_verified_fork(
    board: list[int], size: int, cells: list[int], player: int
) -> tuple[int, int] | None:
    """The double three that still wins after the opponent blocks one line."""
    winning = []
    for cell in cells:
        if _three_three_wins(board, size, cell % size, cell // size, player):
            winning.append(cell)
    if not winning:
        return None
    center = size // 2
    cell = min(winning, key=lambda item: abs(item % size - center) + abs(item // size - center))
    return cell % size, cell // size


def _best_immediate(board: list[int], size: int, player: int) -> tuple[int, int] | None:
    """The winning move, the only saving block, or the richest forced threat.

    Every open four is collected. Each one is kept only if it still wins
    after the opponent replies. Among those, more winning squares and a
    second threat beat a four that merely came first on the board.
    """
    fives = []
    blocks = []
    attacks = []
    forks = []
    denies = []
    other = opponent(player)
    for cell in _nearby(board, size):
        x, y = cell % size, cell // size
        mine = _analyze_move(board, size, x, y, player)
        if mine[0]:
            fives.append((cell, mine))
            continue
        their_shape = _analyze_move(board, size, x, y, other)
        if their_shape[0]:
            blocks.append(cell)
            continue
        if mine[3] >= 1:
            forks.append(cell)
        if their_shape[3] >= 1:
            denies.append(cell)
        if mine[1] or len(mine[5]) >= 2 or _forcing(mine):
            attacks.append((cell, mine))
    if fives:
        fives.sort(key=lambda item: (len(item[1][5]), item[1][1], item[1][3]), reverse=True)
        cell = fives[0][0]
        return cell % size, cell // size
    if len(blocks) > 1:
        return None
    if len(blocks) == 1:
        cell = blocks[0]
        return cell % size, cell // size
    # An open four, or any move with two winning squares, wins before a slower
    # three. Their open four is answered only by a square that removes every
    # such move. The first cell in scan order is not that test.
    our_urgent = [cell for cell, shape in attacks if len(shape[5]) >= 2]
    if not our_urgent:
        our_urgent = _urgent_cells(board, size, player)
    if our_urgent:
        cell = _prefer_attack(board, size, player, our_urgent)
        return cell % size, cell // size
    saves, partial = _urgent_answers(board, size, player)
    if saves:
        cell = _prefer_attack(board, size, player, saves)
        return cell % size, cell // size
    if partial:
        cell = _prefer_attack(board, size, player, partial)
        return cell % size, cell // size
    best = None
    best_key = None
    for cell, shape in attacks:
        if not _reply_holds(board, size, cell, player):
            continue
        x, y = cell % size, cell // size
        center = size // 2
        # More winning squares first. A corner square does not win a tie.
        key = (len(shape[5]), shape[1], shape[2], shape[3], -(abs(x - center) + abs(y - center)))
        if best_key is None or key > best_key:
            best = cell
            best_key = key
    if best is not None:
        return best % size, best // size
    # Two open threes win if each one can still become an open four.
    fork = _best_verified_fork(board, size, forks, player)
    if fork is not None:
        return fork
    # Their double three is the same win, one move later. Take the junction.
    return _best_verified_fork(board, size, denies, other)


def _fives_at(board: list[int], size: int, near: list[int], player: int) -> list[int]:
    """Empty cells in near where player would complete five."""
    found = []
    for index in near:
        if board[index]:
            continue
        if _makes_five(board, size, index % size, index // size, player):
            found.append(index)
    return found


def _has_neighbor(board: list[int], size: int, x: int, y: int) -> bool:
    """A stone lies one or two steps away along a row, column or diagonal."""
    _lines, _through, neigh = _geometry(size)
    index = y * size + x
    return any(board[other] for other in neigh[index])


def _beside_lone_stone(board: list[int], size: int, player: int, x: int, y: int):
    """The reply to a single opening stone has to touch that stone.

    On this board the second player cannot play anywhere else. Searching the
    rest of the grid only produces a click the game will ignore.
    """
    stones = [index for index, value in enumerate(board) if value]
    if len(stones) != 1 or board[stones[0]] == player:
        return x, y
    sx = stones[0] % size
    sy = stones[0] // size
    if max(abs(x - sx), abs(y - sy)) == 1 and board[y * size + x] == EMPTY:
        return x, y
    best = None
    best_key = None
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if dx == 0 and dy == 0:
                continue
            nx, ny = sx + dx, sy + dy
            if not _inside(size, nx, ny) or board[ny * size + nx] != EMPTY:
                continue
            key = (_attack_key(board, size, player, ny * size + nx), dx == 0 or dy == 0)
            if best_key is None or key > best_key:
                best_key = key
                best = (nx, ny)
    if best is None:
        return x, y
    return best


def _guard_move(board: list[int], size: int, player: int, move: tuple[int, int]):
    """Keep a calculated move, but never walk past a five.

    An open four of ours stays an attack. Replacing it with a block would
    throw away the win. Their single five is the one reply that still matters.
    Two fives cannot both be covered, so the calculated move stands.
    """
    x, y = move
    if not _inside(size, x, y) or board[y * size + x] != EMPTY:
        return None
    if _makes_five(board, size, x, y, player):
        return x, y
    ours = []
    threats = []
    other = opponent(player)
    for cell in _nearby(board, size):
        cx, cy = cell % size, cell // size
        if _makes_five(board, size, cx, cy, player):
            ours.append((cx, cy))
        elif _makes_five(board, size, cx, cy, other):
            threats.append((cx, cy))
    if ours:
        return ours[0]
    if len(threats) == 1:
        return threats[0]
    x, y = _beside_lone_stone(board, size, player, x, y)
    if any(board) and not _has_neighbor(board, size, x, y):
        return None
    return x, y


class _Timeout(Exception):
    pass


class _Board:
    """Flat board with a cached pattern score on every line.

    Placing a stone re-scores only the lines through that stone.
    """

    def __init__(self, board: list[int], size: int):
        self.size = size
        self.board = list(board)
        self.lines, self.through, self.neigh = _geometry(size)
        self.black: list[int] = []
        self.white: list[int] = []
        self.long_black_line: list[int] = []
        self.long_white_line: list[int] = []
        for cells in self.lines:
            black, white, black_long, white_long = _score_cells(self.board, cells)
            self.black.append(black)
            self.white.append(white)
            self.long_black_line.append(black_long)
            self.long_white_line.append(white_long)
        self.sum_black = sum(self.black)
        self.sum_white = sum(self.white)
        self.long_black = sum(self.long_black_line)
        self.long_white = sum(self.long_white_line)
        self.near = [0] * (size * size)
        for index, value in enumerate(self.board):
            if value:
                for other in self.neigh[index]:
                    self.near[other] += 1
        self.history: list[int] = []

    def value(self, player: int) -> int:
        """Our threats minus theirs, from the shapes on every line.

        Two long open threes are a fork: the opponent can block only one.
        That bonus stays below an open four, and far below a finished five.
        """
        if player == BLACK:
            base = self.sum_black - self.sum_white
            mine, theirs = self.long_black, self.long_white
        else:
            base = self.sum_white - self.sum_black
            mine, theirs = self.long_white, self.long_black
        if mine >= 2:
            base += 450_000
        if theirs >= 2:
            base -= 450_000
        return base

    def _rescore(self, index: int) -> None:
        for number in self.through[index]:
            self.sum_black -= self.black[number]
            self.sum_white -= self.white[number]
            self.long_black -= self.long_black_line[number]
            self.long_white -= self.long_white_line[number]
            black, white, black_long, white_long = _score_cells(self.board, self.lines[number])
            self.black[number] = black
            self.white[number] = white
            self.long_black_line[number] = black_long
            self.long_white_line[number] = white_long
            self.sum_black += black
            self.sum_white += white
            self.long_black += black_long
            self.long_white += white_long

    def place(self, index: int, player: int) -> None:
        self.board[index] = player
        self._rescore(index)
        for other in self.neigh[index]:
            self.near[other] += 1
        self.history.append(index)

    def undo(self) -> None:
        index = self.history.pop()
        self.board[index] = EMPTY
        self._rescore(index)
        for other in self.neigh[index]:
            self.near[other] -= 1

    def candidates(self, player: int) -> list[int]:
        """Empty cells within two steps of a stone, strongest threat first."""
        return [index for _tier, _score, index in self._ranked(player)]

    def _ranked(self, player: int) -> list[tuple[int, int, int]]:
        rows = []
        center = self.size // 2
        for index, value in enumerate(self.board):
            if value or not self.near[index]:
                continue
            my_long = self.long_black if player == BLACK else self.long_white
            their_long = self.long_white if player == BLACK else self.long_black
            tier, score = _classify(self.board, self.size, index, player, my_long, their_long)
            x = index % self.size
            y = index // self.size
            # Touching more stones beats running away. Closer to the center
            # beats a higher cell number, which used to march into the corner.
            dist = abs(x - center) + abs(y - center)
            rows.append((tier, score, self._touch(index), self._closeness(index), -dist, index))
        rows.sort(key=lambda item: item[:5], reverse=True)
        return [(tier, score, index) for tier, score, _touch, _close, _dist, index in rows]

    def _touch(self, index: int) -> int:
        """How many stones sit one step away, in any of the eight directions."""
        x = index % self.size
        y = index // self.size
        count = 0
        for dx, dy in _RAYS:
            nx = x + dx
            ny = y + dy
            if _inside(self.size, nx, ny) and self.board[ny * self.size + nx]:
                count += 1
        return count

    def _closeness(self, index: int) -> int:
        """2 when a stone is one step away, 1 when the nearest is two steps."""
        x = index % self.size
        y = index // self.size
        best = 0
        for dx, dy in _RAYS:
            for step, rank in ((1, 2), (2, 1)):
                nx = x + dx * step
                ny = y + dy * step
                if not _inside(self.size, nx, ny):
                    break
                if self.board[ny * self.size + nx]:
                    if rank > best:
                        best = rank
                    break
        return best


def _keep_threats(rows: list[tuple[int, int, int]], depth: int, root: bool = False) -> list[int]:
    """Keep wins and saving blocks, then a few verified tactics, then quiet moves.

    The cut applies only after those groups. It stays small on purpose: a
    17×17 board must not grow into dozens of branches at every depth.
    The root looks a little wider, so a quiet position is not two squares.
    """
    wins = []
    blocks = []
    tactical = []
    useful = []
    other = []
    for tier, _score, index in rows:
        if tier >= 5:
            wins.append(index)
        elif tier == 4:
            blocks.append(index)
        elif tier == 3:
            tactical.append(index)
        elif tier == 2:
            useful.append(index)
        else:
            other.append(index)
    if root:
        tactical_cap, useful_cap, other_cap = 8, 6, 6
    elif depth <= 1:
        tactical_cap, useful_cap, other_cap = 8, 6, 4
    elif depth == 2:
        tactical_cap, useful_cap, other_cap = 6, 4, 3
    else:
        tactical_cap, useful_cap, other_cap = 5, 3, 2
    return wins + blocks + tactical[:tactical_cap] + useful[:useful_cap] + other[:other_cap]


def _search(
    pos: _Board,
    player: int,
    depth: int,
    alpha: int,
    beta: int,
    deadline: float | None,
    live=None,
    root: bool = False,
) -> tuple[int, int | None]:
    if live is not None and not live():
        raise _Timeout
    if deadline is not None and time.perf_counter() > deadline:
        raise _Timeout
    finished = winner(pos.board, pos.size)
    if finished:
        return (_MATE if finished == player else -_MATE), None
    other = opponent(player)
    near = [index for index, value in enumerate(pos.board) if not value and pos.near[index]]
    wins = _fives_at(pos.board, pos.size, near, player)
    if wins:
        # A win elsewhere beats a passive block. Prefer the richer winning square.
        if len(wins) > 1:
            wins.sort(key=lambda index: _move_value(pos.board, pos.size, index, player), reverse=True)
        return _MATE, wins[0]
    blocks = _fives_at(pos.board, pos.size, near, other)
    # Two winning squares are independent: one reply cannot cover both.
    if len(blocks) >= 2:
        blocks.sort(key=lambda index: _move_value(pos.board, pos.size, index, player), reverse=True)
        return -_MATE, blocks[0]
    our_urgent = [] if blocks else _urgent_cells(pos.board, pos.size, player)
    if our_urgent:
        # Two winning squares. The opponent can stop only one, then we take five.
        return _MATE - 2, _prefer_attack(pos.board, pos.size, player, our_urgent)
    saves, partial = ([], []) if blocks else _urgent_answers(pos.board, pos.size, player)
    if partial and not saves:
        # No square stops every open four. The game is lost; take the richest try.
        return -(_MATE - 2), _prefer_attack(pos.board, pos.size, player, partial)
    if depth <= 0:
        return pos.value(player), None
    if blocks or saves:
        # Search the saving squares, richest attack first.
        moves = blocks or sorted(saves, key=lambda cell: _attack_key(pos.board, pos.size, player, cell), reverse=True)
    else:
        moves = _keep_threats(pos._ranked(player), depth, root)
    if not moves:
        return 0, None
    best = -_INF
    best_move = moves[0]
    for index in moves:
        pos.place(index, player)
        try:
            score, _reply = _search(pos, other, depth - 1, -beta, -alpha, deadline, live)
        finally:
            pos.undo()
        score = -score
        if score > best:
            best = score
            best_move = index
        if score > alpha:
            alpha = score
        if alpha >= beta:
            break
    return best, best_move



def _star_dots(pixels, width: int, height: int) -> list[tuple[float, float]]:
    """Small gray hoshi dots. Stones are larger, darker, or teal, so they are skipped."""
    seen: set[tuple[int, int]] = set()
    dots = []

    def gray(x: int, y: int) -> bool:
        if x < 0 or y < 0 or x >= width or y >= height:
            return False
        red, green, blue = pixels[x, y][:3]
        bright = (red * 3 + green * 6 + blue) // 10
        if not 110 <= bright <= 190:
            return False
        if max(red, green, blue) - min(red, green, blue) > 50:
            return False
        return green < red + 35

    for y in range(height):
        for x in range(width):
            if (x, y) in seen or not gray(x, y):
                continue
            stack = [(x, y)]
            seen.add((x, y))
            blob = []
            while stack:
                cx, cy = stack.pop()
                blob.append((cx, cy))
                for nx, ny in ((cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)):
                    if (nx, ny) in seen or not gray(nx, ny):
                        continue
                    seen.add((nx, ny))
                    stack.append((nx, ny))
            if not 8 <= len(blob) <= 250:
                continue
            sx = sum(px for px, _py in blob) / len(blob)
            sy = sum(py for _px, py in blob) / len(blob)
            dots.append((sx, sy))
    return dots


def _star_frame(dots: list[tuple[float, float]]):
    """Left, right, top, bottom of the square formed by the outer star dots."""
    if len(dots) < 4:
        return None
    span = max(max(px for px, _py in dots) - min(px for px, _py in dots), 1)
    best = None
    for i, first in enumerate(dots):
        for second in dots[i + 1 :]:
            dx = abs(first[0] - second[0])
            dy = abs(first[1] - second[1])
            if dx < 40 or dy > max(6, dx * 0.08):
                continue
            row_a = (first[1] + second[1]) / 2
            left_a, right_a = sorted((first[0], second[0]))
            for j, third in enumerate(dots):
                for fourth in dots[j + 1 :]:
                    if third in (first, second) or fourth in (first, second):
                        continue
                    dx2 = abs(third[0] - fourth[0])
                    dy2 = abs(third[1] - fourth[1])
                    if abs(dx2 - dx) > max(6, dx * 0.08) or dy2 > max(6, dx * 0.08):
                        continue
                    row_b = (third[1] + fourth[1]) / 2
                    left_b, right_b = sorted((third[0], fourth[0]))
                    if abs(left_a - left_b) > max(6, dx * 0.08) or abs(right_a - right_b) > max(6, dx * 0.08):
                        continue
                    side = (dx + dx2) / 2
                    height = abs(row_a - row_b)
                    if abs(height - side) > max(6, side * 0.08):
                        continue
                    cx = (min(left_a, left_b) + max(right_a, right_b)) / 2
                    cy = (row_a + row_b) / 2
                    centered = any(abs(px - cx) <= side * 0.15 and abs(py - cy) <= side * 0.15 for px, py in dots)
                    score = (1 if centered else 0, side)
                    if best is None or score > best[0]:
                        best = (score, min(left_a, left_b), max(right_a, right_b), min(row_a, row_b), max(row_a, row_b))
    if best is None:
        return None
    return best[1:]


def _period_between(score: list[int], start: float, end: float) -> float | None:
    """Cell size, from the grid strokes that sit between two star dots."""
    left, right = sorted((int(round(start)), int(round(end))))
    local = max(score[left : right + 1], default=0)
    # A real stroke is much darker than the faint echo halfway between lines.
    floor = max(40, int(local * 0.40))
    peaks = []
    for index in range(left + 4, right - 3):
        if score[index] < floor:
            continue
        if score[index] >= score[index - 1] and score[index] > score[index + 1]:
            if peaks and index - peaks[-1] <= 4:
                if score[index] > score[peaks[-1]]:
                    peaks[-1] = index
            else:
                peaks.append(index)
    gaps = [peaks[i + 1] - peaks[i] for i in range(len(peaks) - 1) if peaks[i + 1] - peaks[i] >= 8]
    if len(gaps) < 2:
        return None
    gaps.sort()
    period = float(gaps[len(gaps) // 2])

    def regular(gap: int) -> bool:
        if abs(gap - period) <= max(3, period * 0.25):
            return True
        # One stroke hidden under the opening stone shows up as a double gap.
        return abs(gap - 2 * period) <= max(4, period * 0.35)

    if not all(regular(gap) for gap in gaps):
        return None
    return period


def _expand_axis(score: list[int], anchor: float, step: float) -> list[int] | None:
    """Walk out from a star dot, one cell at a time, while a grid stroke is there."""
    if step < 8:
        return None
    thresh = max(30, int(max(score) * 0.25))

    def snap(pos: float) -> int:
        center = int(round(pos))
        lo = max(1, center - 2)
        hi = min(len(score) - 2, center + 2)
        return max(range(lo, hi + 1), key=lambda index: score[index])

    start = snap(anchor)
    if score[start] < thresh:
        return None
    coords = [start]
    for sign in (1, -1):
        cursor = float(start)
        missed = 0
        while len(coords) < 19:
            guess = cursor + sign * step
            if guess < 1 or guess >= len(score) - 1:
                break
            snapped = snap(guess)
            if score[snapped] >= thresh and abs(snapped - guess) <= 3 and snapped not in coords:
                coords.append(snapped)
                cursor = float(snapped)
                missed = 0
                continue
            # The opening stone can sit on a line and erase that one stroke.
            if missed >= 2:
                break
            far = cursor + sign * 2 * step
            if far < 1 or far >= len(score) - 1:
                break
            beyond = snap(far)
            missing = int(round(cursor + sign * step))
            if (
                score[beyond] < thresh
                or abs(beyond - far) > 3
                or beyond in coords
                or missing in coords
                or not 1 <= missing < len(score) - 1
            ):
                break
            coords.append(missing)
            coords.append(beyond)
            cursor = float(beyond)
            missed += 1
    coords.sort()
    if not 9 <= len(coords) <= 19:
        return None
    return coords


def _grid_from_stars(pixels, width: int, height: int):
    """Build the intersections from the five hoshi dots, then the strokes beyond them."""
    frame = _star_frame(_star_dots(pixels, width, height))
    if frame is None:
        return None
    left, right, top, bottom = frame
    vertical = _line_score(pixels, width, height, vertical=True, gap=2)
    horizontal = _line_score(pixels, width, height, vertical=False, gap=2)
    step_x = _period_between(vertical, left, right)
    step_y = _period_between(horizontal, top, bottom)
    if step_x is None or step_y is None:
        return None
    if abs(step_x - step_y) > max(2.0, step_x * 0.12):
        return None
    step = (step_x + step_y) / 2
    xs = _expand_axis(vertical, left, step)
    ys = _expand_axis(horizontal, top, step)
    if xs is None or ys is None or len(xs) != len(ys):
        return None
    return xs, ys


def _grids_close(a, b, tol: int = 6) -> bool:
    """True when two line lists are the same grid, ignoring a pixel or two of jitter."""
    return len(a) == len(b) and all(abs(p - q) <= tol for p, q in zip(a, b))


def find_grid(image):
    """Return the line columns and rows, or None when no regular grid is visible.

    The five star dots fix the grid when they are visible. Line search is the fallback.
    """
    if image.mode != "RGB":
        image = image.convert("RGB")
    pixels = image.load()
    width, height = image.size
    found = _grid_from_stars(pixels, width, height)
    # A short star grid usually means a stone hid one stroke. The line pass
    # can still see the rest of the board, so keep whichever is larger.
    if found is not None and len(found[0]) >= 15 and len(found[0]) == len(found[1]):
        xs, ys = found
    else:
        line_xs = _lines(pixels, width, height, vertical=True)
        line_ys = _lines(pixels, width, height, vertical=False)
        line = None
        if line_xs and line_ys and len(line_xs) == len(line_ys):
            line = (line_xs, line_ys)
        if found and line:
            xs, ys = line if len(line[0]) > len(found[0]) else found
        elif found:
            xs, ys = found
        elif line:
            xs, ys = line
        else:
            return None
    if not xs or not ys or len(xs) != len(ys):
        return None
    size = len(xs)
    if not 9 <= size <= 19:
        return None
    step = (xs[-1] - xs[0]) / (size - 1)
    if step < 8:
        return None
    return list(xs), list(ys)


def parse_board(image, xs=None, ys=None):
    """Return line columns, line rows, and one value per intersection.

    xs and ys, when given, are reused instead of searching for the grid again.
    Returns None when no regular grid is visible.
    """
    if image.mode != "RGB":
        image = image.convert("RGB")
    pixels = image.load()
    width, height = image.size
    if xs is None or ys is None:
        grid = find_grid(image)
        if grid is None:
            return None
        xs, ys = grid
    if not xs or not ys or len(xs) != len(ys):
        return None
    size = len(xs)
    step = (xs[-1] - xs[0]) / (size - 1)
    if step < 8:
        return None
    board = [_stone(pixels, width, height, x, y, step) for y in ys for x in xs]
    return list(xs), list(ys), board


def _brightness(pixel) -> int:
    red, green, blue = pixel[:3]
    return (red * 3 + green * 6 + blue) // 10


def _line_score(pixels, width: int, height: int, vertical: bool, gap: int) -> list[int]:
    size = width if vertical else height
    other = height if vertical else width
    score = [0] * size
    for along in range(0, other, 2):
        for across in range(size):
            if vertical:
                here = _brightness(pixels[across, along])
                left = _brightness(pixels[across - gap, along]) if across - gap >= 0 else None
                right = _brightness(pixels[across + gap, along]) if across + gap < size else None
            else:
                here = _brightness(pixels[along, across])
                left = _brightness(pixels[along, across - gap]) if across - gap >= 0 else None
                right = _brightness(pixels[along, across + gap]) if across + gap < size else None
            # A line clipped by the crop still has one side on the page.
            if left is None:
                left = right
            if right is None:
                right = left
            if left is None:
                continue
            # Wood strokes are much darker. A web grid is only a few levels
            # under the page. A stone stays dark across its width, so the
            # pixels beside it are not brighter.
            if here + 8 <= left and here + 8 <= right and here < 250:
                score[across] += 1
    return score


def _lines(pixels, width: int, height: int, vertical: bool) -> list[int] | None:
    span = height if vertical else width
    length = width if vertical else height
    best = None
    best_key = None
    # A short gap sees a hairline. A longer gap still sees it when the crop
    # starts on top of the first stroke.
    for gap in (2, 3, 5):
        score = _line_score(pixels, width, height, vertical, gap)
        # The grid may fill only part of the screenshot, so a line only has
        # to show up on a short stretch.
        peaks = _peaks(score, max(6, span // 80))
        chain = _best_chain(peaks, score, length)
        if not chain:
            continue
        gaps = [chain[i + 1] - chain[i] for i in range(len(chain) - 1)]
        med = _median(gaps)
        err = sum(abs(gap_value - med) for gap_value in gaps) / len(gaps)
        # The longest even chain is the grid. A 17-line board must beat a
        # 15-line piece of itself.
        key = (len(chain), -err)
        if best_key is None or key > best_key:
            best_key = key
            best = chain
    return best


def _peaks(score: list[int], floor: int) -> list[int]:
    if not score or max(score) < floor:
        return []
    # A dark window edge can outscore a pale grid. Keep strokes that are
    # only a fraction of that edge, as long as they clear the floor.
    thresh = max(floor, int(max(score) * 0.12))
    centers = []
    index = 0
    last = len(score)
    while index < last:
        if score[index] < thresh:
            index += 1
            continue
        end = index
        while end + 1 < last and score[end + 1] >= thresh:
            end += 1
        mass = sum(score[index : end + 1])
        center = sum(pos * score[pos] for pos in range(index, end + 1)) / mass
        centers.append(int(round(center)))
        index = end + 1
    merged = []
    for center in centers:
        if merged and center - merged[-1] <= 5:
            if score[center] > score[merged[-1]]:
                merged[-1] = center
        else:
            merged.append(center)
    return merged


def _best_chain(peaks: list[int], score: list[int], length: int) -> list[int] | None:
    if len(peaks) < 5:
        return None
    best = None
    best_key = None
    for left in range(len(peaks)):
        for right in range(left + 1, min(left + 6, len(peaks))):
            period = peaks[right] - peaks[left]
            if period < 8 or period > 140:
                continue
            chain = _walk(peaks, peaks[left], period, length, score)
            if not _regular(chain):
                continue
            gaps = [chain[i + 1] - chain[i] for i in range(len(chain) - 1)]
            med = _median(gaps)
            err = sum(abs(gap - med) for gap in gaps) / len(gaps)
            key = (len(chain), -err)
            if best_key is None or key > best_key:
                best_key = key
                best = chain
    return best


def _walk(peaks: list[int], start: int, period: int, length: int, score: list[int]) -> list[int]:
    forward = _direction(peaks, start, period, length, 1, score)
    backward = _direction(peaks, start, period, length, -1, score)
    chain = sorted(set(backward + forward))
    return _extend(chain, length, score)


def _direction(peaks: list[int], start: int, period: int, length: int, sign: int, score: list[int]) -> list[int]:
    chain = [start]
    cursor = start
    missed = 0
    while True:
        nxt = _snap(peaks, cursor + sign * period, period)
        if nxt is not None and (nxt - cursor) * sign > 0:
            chain.append(nxt)
            cursor = nxt
            missed = 0
            continue
        # A stone can hide a whole grid stroke. Bridge one step when the
        # next stroke is still there. Two holes in a row are the border.
        skip = _snap(peaks, cursor + sign * 2 * period, period)
        if skip is not None and (skip - cursor) * sign > 0 and missed < 2:
            missing = cursor + sign * period
            if 1 <= missing < length - 1 and missing not in chain:
                chain.append(missing)
                chain.append(skip)
                cursor = skip
                missed += 1
                continue
        break
    return chain


def _snap(peaks: list[int], target: int, period: int) -> int | None:
    tol = max(3, int(period * 0.22))
    near = [peak for peak in peaks if abs(peak - target) <= tol]
    if not near:
        return None
    return min(near, key=lambda peak: abs(peak - target))


def _extend(chain: list[int], length: int, score: list[int]) -> list[int]:
    if len(chain) < 2:
        return chain
    gaps = [chain[i + 1] - chain[i] for i in range(len(chain) - 1)]
    period = int(round(_median(gaps)))
    thresh = max(score) * 0.30
    for _ in range(3):
        nxt = chain[-1] + period
        if nxt >= length - 2 or not _supported(score, nxt, thresh):
            break
        chain.append(nxt)
    for _ in range(3):
        prv = chain[0] - period
        if prv <= 1 or not _supported(score, prv, thresh):
            break
        chain.insert(0, prv)
    return chain


def _supported(score: list[int], pos: int, thresh: float) -> bool:
    window = score[max(0, pos - 2) : min(len(score), pos + 3)]
    return bool(window) and max(window) >= thresh


def _regular(chain: list[int] | None) -> bool:
    if not chain or not 9 <= len(chain) <= 19:
        return False
    gaps = [chain[i + 1] - chain[i] for i in range(len(chain) - 1)]
    med = _median(gaps)
    if med < 8:
        return False
    limit = max(4, med * 0.28)
    return all(abs(gap - med) <= limit for gap in gaps)


def _median(values: list[int]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2


def _is_teal(red: int, green: int, blue: int) -> bool:
    """The pale-board 'white' stone is teal: green leads, and it is not navy."""
    return green > red + 35 and green > 90 and green + 20 >= blue


def _is_ink(red: int, green: int, blue: int) -> bool:
    """A black stone is dark and is not the teal side."""
    bright = (red * 3 + green * 6 + blue) // 10
    return bright < 130 and green < red + 30


# Twelve points inside the stone, off the grid cross, then four on the rim.
_BODY = ((1, 1), (1, -1), (-1, 1), (-1, -1), (2, 1), (2, -1), (-2, 1), (-2, -1), (1, 2), (1, -2), (-1, 2), (-1, -2))
_RIM = ((1, 1), (1, -1), (-1, 1), (-1, -1))


def _probe(pixels, width: int, height: int, x: int, y: int, radius: int) -> list[tuple[int, int, int]]:
    colors = []
    for dx, dy in _BODY:
        pixel = _at(pixels, width, height, x + (dx * radius) // 2, y + (dy * radius) // 2)
        if pixel is not None:
            colors.append(pixel[:3])
    return colors


def _stone(pixels, width: int, height: int, x: int, y: int, step: float) -> int:
    background = _background(pixels, width, height, x, y, step)
    if background is None:
        return EMPTY
    radius = max(4, int(step * 0.28))
    colors = _probe(pixels, width, height, x, y, radius)
    if len(colors) < 8:
        return EMPTY
    teal = sum(1 for red, green, blue in colors if _is_teal(red, green, blue))
    ink = sum(1 for red, green, blue in colors if _is_ink(red, green, blue))
    count = len(colors)
    # Teal is the O on this board. It is darker than the page, so brightness
    # alone would call it black and glue both colors into one line of five.
    if teal >= 4 and teal >= ink:
        return WHITE
    if ink >= 5 and ink > teal:
        return BLACK
    bright = sorted(_brightness(color) for color in colors)
    median = bright[count // 2]
    dark = sum(value < background - 35 for value in bright)
    if dark * 2 > count and median < background - 28 and ink >= teal:
        return BLACK
    # White stones on a pale page are only a little brighter than the wood.
    lifted = sum(value >= background + 5 for value in bright)
    if lifted * 2 >= count and median >= background + 5 and dark * 4 < count:
        return WHITE
    # Same color as the page, but the stone still has a gray rim.
    if _rim_dark(pixels, width, height, x, y, step, background) and median >= background - 12 and dark * 3 < count:
        return WHITE
    return EMPTY


def _rim_dark(pixels, width, height, x, y, step, background) -> bool:
    """True when the four diagonal points on the stone edge are darker than the page."""
    radius = max(5, int(step * 0.42))
    shift = int(round(radius * 0.72))
    darker = 0
    seen = 0
    for dx, dy in _RIM:
        pixel = _at(pixels, width, height, x + dx * shift, y + dy * shift)
        if pixel is None:
            continue
        seen += 1
        if _brightness(pixel) < background - 22:
            darker += 1
    return seen >= 3 and darker >= 3


def _background(pixels, width, height, x, y, step):
    offset = max(5, int(round(step * 0.42)))
    samples = []
    for dx, dy in ((offset, offset), (offset, -offset), (-offset, offset), (-offset, -offset)):
        pixel = _at(pixels, width, height, x + dx, y + dy)
        if pixel is not None:
            samples.append(_brightness(pixel))
    if not samples:
        return None
    samples.sort()
    return samples[len(samples) // 2]


def _at(pixels, width, height, x, y):
    if x < 0 or y < 0 or x >= width or y >= height:
        return None
    return pixels[x, y]


def enable_dpi():
    """Use real pixels so the box, the screenshot and the click land together.

    This has to run before pyautogui is imported. pyautogui switches the process
    to the older system-wide mode, and Windows then ignores any later change.
    """
    try:
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


enable_dpi()

try:
    import pyautogui
except ImportError:
    pyautogui = None


def virtual_screen() -> tuple[int, int, int, int]:
    metrics = ctypes.windll.user32.GetSystemMetrics
    return metrics(76), metrics(77), metrics(78), metrics(79)


def grab(bbox):
    """Screenshot in screen pixels, including a monitor left of or above the main one."""
    left, top, right, bottom = bbox
    main_w = ctypes.windll.user32.GetSystemMetrics(0)
    main_h = ctypes.windll.user32.GetSystemMetrics(1)
    if left >= 0 and top >= 0 and right <= main_w and bottom <= main_h:
        return ImageGrab.grab(bbox=bbox)
    return ImageGrab.grab(bbox=bbox, all_screens=True)


def select_region() -> tuple[int, int, int, int] | None:
    root = tk.Tk()
    vx, vy, vw, vh = virtual_screen()
    root.overrideredirect(True)
    root.geometry(f"{vw}x{vh}+{vx}+{vy}")
    root.attributes("-topmost", True)
    root.attributes("-alpha", 0.25)
    root.configure(bg="black")
    canvas = tk.Canvas(root, bg="black", highlightthickness=0, cursor="cross")
    canvas.pack(fill="both", expand=True)
    root.focus_force()
    main_w = ctypes.windll.user32.GetSystemMetrics(0)
    canvas.create_text(
        main_w // 2 - vx,
        40 - vy,
        text="Kéo một khung chứa cả bàn cờ. Thừa viền cũng được. Esc để hủy.",
        fill="white",
        font=("Segoe UI", 16),
    )
    state = {"x0": 0, "y0": 0, "rect": None, "result": None}

    def press(event):
        state["x0"] = event.x_root
        state["y0"] = event.y_root
        if state["rect"] is not None:
            canvas.delete(state["rect"])
        state["rect"] = canvas.create_rectangle(event.x, event.y, event.x, event.y, outline="#00ff66", width=2)

    def drag(event):
        if state["rect"] is None:
            return
        canvas.coords(
            state["rect"],
            state["x0"] - root.winfo_rootx(),
            state["y0"] - root.winfo_rooty(),
            event.x,
            event.y,
        )

    def release(event):
        x0, y0 = state["x0"], state["y0"]
        x1, y1 = event.x_root, event.y_root
        left, right = sorted((x0, x1))
        top, bottom = sorted((y0, y1))
        if right - left > 40 and bottom - top > 40:
            state["result"] = (left, top, right - left, bottom - top)
        root.destroy()

    canvas.bind("<ButtonPress-1>", press)
    canvas.bind("<B1-Motion>", drag)
    canvas.bind("<ButtonRelease-1>", release)
    root.bind("<Escape>", lambda _event: root.destroy())
    root.mainloop()
    return state["result"]


def make_clickthrough(window):
    window.update_idletasks()
    hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
    style = ctypes.windll.user32.GetWindowLongW(hwnd, -20)
    ctypes.windll.user32.SetWindowLongW(hwnd, -20, style | 0x00080000 | 0x00000020)


def hide_from_capture(window) -> bool:
    """Ask Windows to leave this window out of screenshots (Windows 10 2004+)."""
    window.update_idletasks()
    hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
    try:
        return bool(ctypes.windll.user32.SetWindowDisplayAffinity(hwnd, 0x00000011))
    except Exception:
        return False


PROBE_COLOR = (0, 255, 102)


def capture_ignores(window, canvas, region) -> bool:
    """Draw a probe and check that a screenshot does not see it."""
    _x, _y, width, height = region
    size = max(6, min(24, width // 10, height // 10))
    probe = canvas.create_rectangle(0, 0, size, size, fill="#00ff66", outline="")
    window.deiconify()
    window.lift()
    window.update()
    time.sleep(0.12)
    try:
        left = window.winfo_rootx()
        top = window.winfo_rooty()
        shot = grab((left, top, left + size, top + size))
        pixels = shot.load()
        for py in range(shot.height):
            for px in range(shot.width):
                red, green, blue = pixels[px, py][:3]
                if (
                    abs(red - PROBE_COLOR[0]) < 40
                    and abs(green - PROBE_COLOR[1]) < 40
                    and abs(blue - PROBE_COLOR[2]) < 40
                ):
                    return False
        return True
    except Exception:
        return False
    finally:
        canvas.delete(probe)
        window.update()


def render(board: list[int], size: int, move: tuple[int, int] | None) -> str:
    mx, my = move if move else (-1, -1)
    lines = []
    for y in range(size):
        chars = []
        for x in range(size):
            value = board[y * size + x]
            if (x, y) == (mx, my) and value == EMPTY:
                chars.append("+")
            elif value == BLACK:
                chars.append("X")
            elif value == WHITE:
                chars.append("O")
            else:
                chars.append(".")
        lines.append(" ".join(chars))
    return "\n".join(lines)


class Watcher:
    def __init__(self, region, side: str, depth: int):
        self.region = region
        self.side = side
        self.depth = depth
        self.xs: list[int] | None = None
        self.ys: list[int] | None = None
        self.full_xs: list[int] | None = None
        self.full_ys: list[int] | None = None
        self.trim = 0
        self.pending: tuple | None = None
        self.stable: tuple | None = None
        self.prev_raw: tuple | None = None
        self._cell_value: list | None = None
        self._cell_at: list[float] | None = None
        self.grid_at = 0.0
        self.alt: tuple | None = None
        self._grid_busy = False
        self._grid_found = None
        self._grid_gen = 0
        self.matched_at: float | None = None
        self.changed_at = time.perf_counter()
        self.acted_at = 0.0
        self.acted_board: tuple | None = None
        self.retries = 0
        self.misses = 0
        self.guess_misses = 0
        self.opener = BLACK
        self.seat = _Seat()
        self.playing = None
        self.move_ready_at = 0.0
        self.move_fast = False
        self._ready_key = None
        self.skip_until = 0.0
        self.note = ""
        self._lock = threading.Lock()
        self._gen = 0
        self._worker_key = None
        self._result_key = None
        self._result_move = None
        self._result_fast = False
        self.running = True
        self.auto = False
        self.f2_down = False
        self.last_marks = None
        x, y, width, height = region

        self.panel = tk.Tk()
        self.panel.title("Cờ caro — đọc bàn")
        self.overlay = tk.Toplevel(self.panel)
        self.overlay.overrideredirect(True)
        self.overlay.attributes("-topmost", True)
        self.overlay.attributes("-transparentcolor", "magenta")
        self.overlay.geometry(f"{width}x{height}+{x}+{y}")
        self.overlay.configure(bg="magenta")
        self.canvas = tk.Canvas(self.overlay, bg="magenta", highlightthickness=0, width=width, height=height)
        self.canvas.pack()
        self.overlay.update_idletasks()
        make_clickthrough(self.overlay)
        self.quiet = hide_from_capture(self.overlay) and capture_ignores(self.overlay, self.canvas, region)
        self.interval = 40 if self.quiet else 70

        if pyautogui is not None:
            pyautogui.FAILSAFE = True
            pyautogui.PAUSE = 0

        self.panel.attributes("-topmost", True)
        self.panel.geometry(self._panel_geometry(560, 520))
        mode = "Khung gợi ý không bị chụp vào ảnh." if self.quiet else "Windows cũ: khung gợi ý phải ẩn lúc đọc, sẽ hơi chớp."
        self.status = tk.StringVar(value=f"Đang tìm lưới. {mode}")
        self.auto_text = tk.StringVar()
        self.side_var = tk.StringVar(value=side)
        self.deep = tk.BooleanVar(value=False)
        self.show_stones = tk.BooleanVar(value=True)

        tk.Label(self.panel, textvariable=self.auto_text, anchor="w", font=("Segoe UI", 10, "bold")).pack(
            fill="x", padx=8, pady=(8, 0)
        )
        tk.Label(self.panel, textvariable=self.status, anchor="w", justify="left", wraplength=530).pack(fill="x", padx=8)
        tk.Label(
            self.panel,
            text="X đen    O trắng    + nước gợi ý    ·    vòng xanh dương / vàng là quân tool đang thấy",
            anchor="w",
        ).pack(fill="x", padx=8)

        options = tk.Frame(self.panel)
        options.pack(side="bottom", pady=(0, 8))
        tk.Label(options, text="Bên đi:").pack(side="left", padx=(4, 0))
        for value, label in (("auto", "Tự động"), ("first", "Trước"), ("second", "Sau")):
            tk.Radiobutton(options, text=label, variable=self.side_var, value=value, command=self._on_side).pack(
                side="left"
            )
        tk.Checkbutton(options, text="Nghĩ kỹ hơn", variable=self.deep, command=self._invalidate).pack(side="left", padx=8)
        tk.Checkbutton(options, text="Hiện quân đã đọc", variable=self.show_stones, command=self._redraw).pack(side="left")

        buttons = tk.Frame(self.panel)
        buttons.pack(side="bottom", pady=(0, 4))
        tk.Button(buttons, text="Đọc lại lưới", command=self.reset_grid).pack(side="left", padx=4)
        tk.Button(buttons, text="Bỏ đường viền", command=self.drop_border).pack(side="left", padx=4)
        self.auto_button = tk.Button(buttons, command=self.toggle_auto, width=20)
        self.auto_button.pack(side="left", padx=4)
        tk.Button(buttons, text="Dừng", command=self.stop).pack(side="left", padx=4)

        self.board = tk.Text(self.panel, height=20, width=42, font=("Consolas", 11), wrap="none")
        self.board.pack(fill="both", expand=True, padx=8, pady=8)
        self.board.bind("<Key>", lambda _event: "break")
        self._show_auto_state()
        self.panel.protocol("WM_DELETE_WINDOW", self.stop)
        self.panel.bind("<Escape>", lambda _event: self.stop())
        self.panel.after(200, self.tick)
        self.panel.after(40, self.poll_keys)

    def _panel_geometry(self, panel_w: int, panel_h: int) -> str:
        x, y, width, height = self.region
        vx, vy, vw, vh = virtual_screen()
        gap = 16
        spots = (
            (x + width + gap, y),
            (x - panel_w - gap, y),
            (x, y + height + gap + 40),
            (x, y - panel_h - gap - 40),
            (vx + 20, vy + 20),
        )
        for px, py in spots:
            inside = vx <= px and vy <= py and px + panel_w <= vx + vw and py + panel_h + 40 <= vy + vh
            overlap = px < x + width and x < px + panel_w and py < y + height and y < py + panel_h + 40
            if inside and not overlap:
                return f"{panel_w}x{panel_h}+{px}+{py}"
        return f"{panel_w}x{panel_h}+{vx + 20}+{vy + 20}"

    def _show_auto_state(self):
        if self.auto:
            self.auto_text.set("TỰ ĐỘNG: BẬT · đang xem mình đi trước hay sau")
            self.auto_button.configure(text="Tắt tự động (F2)")
        else:
            self.auto_text.set("TỰ ĐỘNG: TẮT  (bấm F2 để bật)")
            self.auto_button.configure(text="Bật tự động (F2)")

    def _invalidate(self):
        with self._lock:
            self._gen += 1
            self._result_key = None
            self._worker_key = None

    def _on_side(self):
        self._forget_seat()
        self._invalidate()

    def _forget_seat(self):
        self.seat.reset(time.perf_counter())
        self.playing = None
        self.note = ""
        self.misses = 0
        self.guess_misses = 0
        self.retries = 0
        self.skip_until = 0.0
        self._ready_key = None

    def _redraw(self):
        self.last_marks = None

    def poll_keys(self):
        if not self.running:
            return
        state = ctypes.windll.user32.GetAsyncKeyState
        down = bool(state(0x71) & 0x8000)
        modifier = any(state(key) & 0x8000 for key in (0x11, 0x12, 0x5B, 0x5C))
        if down and not self.f2_down and not modifier:
            self.toggle_auto()
        self.f2_down = down
        self.panel.after(40, self.poll_keys)

    def _f2_pressed(self) -> bool:
        return bool(ctypes.windll.user32.GetAsyncKeyState(0x71) & 0x8000)

    def reset_grid(self):
        self.xs = None
        self.ys = None
        self.full_xs = None
        self.full_ys = None
        self.pending = None
        self.stable = None
        self.prev_raw = None
        self.grid_at = 0.0
        self.alt = None
        with self._lock:
            self._grid_gen += 1
            self._grid_found = None
        self.matched_at = None
        self.acted_board = None
        self.retries = 0
        self.opener = BLACK
        self.last_marks = None
        self._forget_seat()
        self._invalidate()
        self.status.set("Đang tìm lại lưới")

    def _trimmed(self, xs, ys) -> tuple[list[int], list[int]] | None:
        """Drop trim layers of outermost lines. None when fewer than 9 lines would remain."""
        layers = self.trim
        if len(xs) - 2 * layers < 9 or len(ys) - 2 * layers < 9:
            return None
        if layers <= 0:
            return list(xs), list(ys)
        return list(xs)[layers:-layers], list(ys)[layers:-layers]

    def _lock_grid(self, xs, ys) -> None:
        self.full_xs = list(xs)
        self.full_ys = list(ys)
        trimmed = self._trimmed(xs, ys)
        self.xs, self.ys = trimmed if trimmed is not None else (list(xs), list(ys))
        self.stable = None
        self.prev_raw = None
        self.matched_at = None
        self.pending = None

    def drop_border(self):
        """Drop the outermost line on every side, once."""
        if not self.full_xs or not self.full_ys:
            self.status.set("Chưa có lưới. Hãy khoanh bàn trước.")
            return
        if self.trim >= 1:
            self.status.set(f"Đã bỏ đường viền. Bàn đang {len(self.xs)}×{len(self.ys)}.")
            return
        self.trim = 1
        trimmed = self._trimmed(self.full_xs, self.full_ys)
        if trimmed is None:
            self.trim = 0
            self.status.set("Không bỏ được vì bàn còn quá ít đường.")
            return
        self.xs, self.ys = trimmed
        self.alt = None
        self.stable = None
        self.prev_raw = None
        self.matched_at = None
        self.pending = None
        self.last_marks = None
        self._forget_seat()
        self._invalidate()
        self.status.set(f"Đã bỏ đường viền. Còn {len(self.xs)}×{len(self.ys)}.")

    def toggle_auto(self):
        if pyautogui is None:
            self.status.set("Chưa cài pyautogui. Chạy: pip install pyautogui")
            return
        self.auto = not self.auto
        self.acted_board = None
        self.acted_at = 0.0
        self._forget_seat()
        self._invalidate()
        self._show_auto_state()
        # A hover preview on the intersection would be read as a real stone.
        if self.auto:
            self._park_mouse()

    def stop(self):
        self.running = False
        try:
            self.panel.destroy()
        except tk.TclError:
            pass

    def tick(self):
        if not self.running:
            return
        try:
            self._read_and_mark()
        except Exception as error:
            self.status.set(f"Lỗi đọc màn hình: {error}")
        if self.running:
            self.panel.after(self.interval, self.tick)

    def _show_overlay(self):
        x, y, width, height = self.region
        if self.overlay.state() == "withdrawn":
            self.overlay.deiconify()
            self.overlay.geometry(f"{width}x{height}+{x}+{y}")
        self.overlay.lift()
        self.overlay.update_idletasks()

    def _grid_worker(self, image, gen: int) -> None:
        try:
            found = find_grid(image)
        except Exception:
            found = None
        with self._lock:
            if gen == self._grid_gen:
                self._grid_found = found
            self._grid_busy = False

    def _take_grid(self) -> None:
        with self._lock:
            found = self._grid_found
            self._grid_found = None
        if not found:
            return
        nxs, nys = found
        if self.full_xs is None:
            self._lock_grid(nxs, nys)
            return
        # A stone on the opening point can hide a line. Never trade the full
        # board for that shorter reading.
        if len(nxs) < len(self.full_xs) or len(nys) < len(self.full_ys):
            self.alt = None
            return
        if _grids_close(nxs, self.full_xs) and _grids_close(nys, self.full_ys):
            self.alt = None
            return
        if self.alt is not None and _grids_close(nxs, self.alt[0]) and _grids_close(nys, self.alt[1]):
            self.alt = None
            self._lock_grid(nxs, nys)
            return
        self.alt = (list(nxs), list(nys))

    def _sample(self, image):
        """Read stones on the locked grid.

        Searching for the lines again runs beside the stone read, so a slow
        grid pass cannot hold up the next stone.
        """
        self._take_grid()
        now = time.perf_counter()
        if self.xs is None:
            grid = find_grid(image)
            if grid is None:
                return None
            self._lock_grid(*grid)
            self.grid_at = now
            return parse_board(image, self.xs, self.ys)
        start_grid = False
        gen = 0
        if now - self.grid_at >= 4.0:
            with self._lock:
                if not self._grid_busy and now - self.grid_at >= 4.0:
                    self._grid_busy = True
                    gen = self._grid_gen
                    start_grid = True
        if start_grid:
            self.grid_at = now
            threading.Thread(target=self._grid_worker, args=(image.copy(), gen), daemon=True).start()
        return parse_board(image, self.xs, self.ys)

    def _absorb(self, raw: tuple) -> bool:
        """Confirm each intersection on its own.

        A stone counts after it stays put for a short stretch. A cell that
        only flickers — the mark on the opening stone, a hint on a neighbor —
        keeps its last confirmed value. That flicker used to hold the whole
        board, so the second player never finished the read.
        """
        now = time.perf_counter()
        count = len(raw)
        if (
            self.stable is None
            or self._cell_at is None
            or self._cell_value is None
            or len(self.stable) != count
            or len(self._cell_at) != count
        ):
            self.stable = tuple(EMPTY for _ in range(count))
            self._cell_value = list(raw)
            self._cell_at = [now] * count
            self.prev_raw = raw
            self.changed_at = now
            self.matched_at = None
            return False

        stable = list(self.stable)
        changed = False
        pending = False
        fresh = False
        for index, value in enumerate(raw):
            if value != self._cell_value[index]:
                self._cell_value[index] = value
                self._cell_at[index] = now
                if value != stable[index]:
                    fresh = True
                continue
            if value == stable[index]:
                continue
            held = now - self._cell_at[index]
            if held < 0.20:
                fresh = True
                if held >= 0.10:
                    pending = True
                continue
            stable[index] = value
            changed = True
        self.prev_raw = raw
        if changed:
            self.stable = tuple(stable)
            self.changed_at = now
            self.matched_at = None
            return False
        if pending:
            self.matched_at = None
            return False
        # A one-frame flicker does not restart the wait. A stone that is
        # still arriving does pause it, so we never play on the old board.
        if fresh:
            return False
        if self.matched_at is None:
            self.matched_at = now
            return False
        return now - self.matched_at >= 0.28

    def _read_and_mark(self):
        if not self.quiet:
            self.overlay.withdraw()
            self.panel.update()
            time.sleep(0.02)
        x, y, width, height = self.region
        image = grab((x, y, x + width, y + height))
        parsed = self._sample(image)
        if parsed is None:
            self.status.set("Chưa thấy lưới. Kéo khung rộng hơn, phủ hết bàn cờ, rồi bấm Đọc lại lưới.")
            self._show_overlay()
            return
        xs, ys, board = parsed
        size = len(xs)
        self.interval = 20 if self.quiet else 40
        settled = self._absorb(tuple(board))
        shown = list(self.stable if self.stable is not None else board)
        if settled:
            self._confirm_click(tuple(shown))

        move, advice = self._advise(shown, size, settled)
        picture = render(shown, size, move)
        self._show_overlay()
        shift_x = x - self.overlay.winfo_rootx()
        shift_y = y - self.overlay.winfo_rooty()
        scale_x = width / image.width
        scale_y = height / image.height
        marks = (move, shift_x, shift_y, tuple(xs), tuple(ys), tuple(shown), self.show_stones.get())
        if marks != self.last_marks:
            self.last_marks = marks
            self._draw_marks(shown, move, xs, ys, scale_x, scale_y, shift_x, shift_y)
        current = self.board.get("1.0", "end-1c")
        if current != picture:
            self.board.delete("1.0", "end")
            self.board.insert("end", picture)
        self._auto_play(move, advice, settled, shown, xs, ys, scale_x, scale_y)
        text = f"{size}×{size}. {advice}"
        if self.note:
            text = f"{text} {self.note}"
        self.status.set(text)

    def _think(self, board: list[int], size: int, player: int, depth: int, budget: float | None, key, gen: int, complete: bool) -> None:
        def live() -> bool:
            return self._gen == gen

        started = time.perf_counter()
        try:
            move = choose_move(board, size, player, depth, budget, complete=complete, live=live)
        except Exception:
            move = None
        fast = move is not None and time.perf_counter() - started < 0.25
        with self._lock:
            if self._gen == gen and self._worker_key == key:
                self._result_key = key
                self._result_move = move
                self._result_fast = fast
            if self._worker_key == key:
                self._worker_key = None

    def _banner(self, player, opener) -> None:
        if not self.auto:
            return
        if player is None:
            self.auto_text.set("TỰ ĐỘNG: BẬT · đang xem mình đi trước hay sau")
            return
        self.auto_text.set(f"TỰ ĐỘNG: BẬT · mình {_seat(player, opener)}")

    def _advise(self, board: list[int], size: int, settled: bool) -> tuple[tuple[int, int] | None, str]:
        done = winner(board, size)
        if done == BLACK:
            self.playing = None
            return None, "Đen đã có năm quân."
        if done == WHITE:
            self.playing = None
            return None, "Trắng đã có năm quân."
        turn, opener, black, white = whose_turn(board, self.opener)
        counts = f"Đen {black}, trắng {white}."
        if not settled:
            self.playing = None
            return None, f"{counts} Đang chốt quân trên bàn…"
        if turn is None:
            self.playing = None
            return None, f"{counts} Số quân lệch, đọc lại bàn."
        self.opener = opener
        choice = self.side_var.get()
        if choice == "first":
            player = opener
            phrase = ""
        elif choice == "second":
            player = opponent(opener)
            phrase = ""
        elif self.auto:
            player, phrase = resolve_seat(self.seat, board, turn, opener, True, time.perf_counter())
        else:
            player, phrase = turn, ""
        self.playing = player
        self._banner(player, opener)
        if player is None:
            return None, f"{counts} {phrase}"
        if turn != player:
            if self.auto:
                return None, f"{counts} Mình {_seat(player, opener)}. Đang chờ đối thủ suy nghĩ."
            return None, f"{counts} Đến lượt {_seat(turn, opener)}."
        # Auto spends the full budget. A quiet position is searched, not skipped.
        # "Nghĩ kỹ hơn" looks one step further and still stops on the clock.
        deep = self.deep.get()
        if deep:
            depth, budget, complete = min(max(self.depth, 4) + 2, 8), 3.0, True
        elif self.auto:
            depth, budget, complete = min(max(self.depth, 5), 8), 2.0, True
        else:
            depth, budget, complete = min(self.depth, 4), 1.2, False
        key = (tuple(board), player, depth, budget, complete)
        with self._lock:
            if self._result_key == key:
                move = self._result_move
                fast = self._result_fast
                ready = True
            else:
                move = None
                fast = False
                ready = False
                if self._worker_key != key:
                    self._gen += 1
                    gen = self._gen
                    self._worker_key = key
                    self._result_key = None
                    threading.Thread(
                        target=self._think,
                        args=(list(board), size, player, depth, budget, key, gen, complete),
                        daemon=True,
                    ).start()
        if ready and self._ready_key != key:
            self._ready_key = key
            self.move_ready_at = time.perf_counter()
            self.move_fast = fast
        seat = _seat(player, opener)
        mine = f"Mình {seat}. " if self.auto else f"Lượt {seat}. "
        if not ready:
            verb = "Đang nghĩ kỹ" if deep else "Đang tính nước"
            return None, f"{counts} {mine}{verb}…"
        if move is None:
            return None, f"{counts} {mine}Không có nước."
        return move, f"{counts} {mine}Gợi ý hàng {move[1] + 1}, cột {move[0] + 1}."

    def _draw_marks(self, board, move, xs, ys, scale_x, scale_y, shift_x, shift_y):
        self.canvas.delete("all")
        size = len(xs)
        step = (xs[-1] - xs[0]) / (size - 1)
        radius = max(4, step * 0.28 * min(scale_x, scale_y))
        if self.show_stones.get():
            for row, line_y in enumerate(ys):
                for col, line_x in enumerate(xs):
                    stone = board[row * size + col]
                    if stone == EMPTY:
                        continue
                    cx = line_x * scale_x + shift_x
                    cy = line_y * scale_y + shift_y
                    color = "#29b6f6" if stone == BLACK else "#ffb300"
                    self.canvas.create_oval(cx - radius, cy - radius, cx + radius, cy + radius, outline=color, width=2)
        if move is None:
            return
        col, row = move
        cx = xs[col] * scale_x + shift_x
        cy = ys[row] * scale_y + shift_y
        self.canvas.create_oval(cx - radius, cy - radius, cx + radius, cy + radius, outline="#00e676", width=3)
        self.canvas.create_oval(cx - 4, cy - 4, cx + 4, cy + 4, fill="#00e676", outline="")

    def _point(self, move, xs, ys, scale_x, scale_y) -> tuple[int, int]:
        col, row = move
        origin_x, origin_y, _width, _height = self.region
        return round(origin_x + xs[col] * scale_x), round(origin_y + ys[row] * scale_y)

    def _park_mouse(self):
        origin_x, origin_y, width, height = self.region
        vx, vy, vw, vh = virtual_screen()
        candidates = (
            (origin_x + width + 24, origin_y + 12),
            (origin_x - 24, origin_y + 12),
            (origin_x + 12, origin_y + height + 24),
            (origin_x + 12, origin_y - 24),
        )
        for px, py in candidates:
            on_board = origin_x <= px < origin_x + width and origin_y <= py < origin_y + height
            on_screen = vx + 12 <= px <= vx + vw - 12 and vy + 12 <= py <= vy + vh - 12
            if on_screen and not on_board:
                pyautogui.moveTo(px, py)
                return
        pyautogui.moveTo(vx + 12, vy + 12)

    def _click(self, move, xs, ys, scale_x, scale_y):
        sx, sy = self._point(move, xs, ys, scale_x, scale_y)
        pyautogui.moveTo(sx, sy)
        time.sleep(0.05)
        pyautogui.mouseDown(sx, sy, button="left")
        time.sleep(0.05)
        pyautogui.mouseUp(sx, sy, button="left")

    def _lock_our(self, color: int, board) -> None:
        self.seat.color = color
        self.seat.confirmed = True
        self.seat.assumed = False
        self.seat.waiting = False
        self.seat.allow = False
        self.seat.pending = None
        self.seat.watch = tuple(board)
        self.misses = 0
        self.guess_misses = 0
        self.retries = 0
        self.note = ""
        self.acted_board = None

    def _confirm_click(self, board: tuple) -> None:
        """A stone on the clicked intersection locks our color. Anything else does not."""
        pending = self.seat.pending
        if pending is None or pending >= len(board):
            self.seat.pending = None
            return
        if board[pending] in (BLACK, WHITE):
            if self.side_var.get() == "auto":
                self._lock_our(board[pending], board)
            else:
                self.seat.pending = None
                self.misses = 0
                self.retries = 0
                self.note = ""
                self.acted_board = None
            return
        if self.acted_board is None or board == self.acted_board:
            return
        # The table changed, but not on the square we clicked.
        self.seat.pending = None
        self.acted_board = None
        self.retries = 0
        if self.seat.confirmed or self.side_var.get() != "auto":
            return
        self.seat.color = None
        self.seat.assumed = False
        self.seat.waiting = False
        self.seat.allow = True
        self.seat.watch = board
        self.seat.armed = time.perf_counter()
        self.guess_misses = 0

    def _click_failed(self, snapshot: tuple) -> None:
        now = time.perf_counter()
        self.retries = 0
        self.seat.pending = None
        self.acted_board = None
        if self.seat.confirmed or self.side_var.get() != "auto":
            self.misses += 1
            self.skip_until = now + 1.2
            if self.misses >= 4:
                self.auto = False
                self.note = ""
                self._show_auto_state()
                self.status.set("Không đặt được quân. Đã tắt tự động.")
                return
            self.note = "Chưa đặt được quân. Sẽ thử lại."
            return
        self.guess_misses += 1
        self.seat.color = None
        self.seat.assumed = False
        self.seat.waiting = True
        self.seat.allow = False
        self.seat.watch = snapshot
        # A missed opening click is retried. After that, wait until the
        # table actually changes, so a wrong-seat guess does not keep clicking.
        if not any(snapshot) and self.guess_misses < 3:
            self.seat.retry_at = now + 0.8
            self.note = "Chưa đặt được quân. Thử lại."
            return
        if any(snapshot) and self.guess_misses < 2:
            self.seat.retry_at = now + _RETRY_GAP
            self.note = "Click chưa ăn. Đang chờ đối thủ."
            return
        self.seat.retry_at = now + 10_000
        self.note = "Đang chờ đối thủ đi."

    def _auto_play(self, move, advice, settled, shown, xs, ys, scale_x, scale_y):
        # settled means every intersection has agreed for a stretch. move is
        # None until it is our turn and the search for that exact board is done.
        if not self.auto or not self.running or pyautogui is None or not settled or move is None:
            return
        if advice.startswith("Đen đã") or advice.startswith("Trắng đã"):
            return
        now = time.perf_counter()
        if now < self.skip_until or self.move_ready_at <= 0:
            return
        stable_for = now - self.changed_at
        ready_for = now - self.move_ready_at
        if stable_for < (0.32 if self.move_fast else 0.45):
            return
        if ready_for < (0.12 if self.move_fast else 0.28):
            return
        snapshot = tuple(shown)
        if snapshot == self.acted_board:
            if now - self.acted_at < 0.7:
                return
            sure = self.seat.confirmed or self.side_var.get() != "auto"
            if not sure or self.retries >= 1:
                self._click_failed(snapshot)
                return
            self.retries += 1
        else:
            if now - self.acted_at < 0.2:
                return
            self.retries = 0
        if self._f2_pressed():
            return
        turn, _opener, _black, _white = whose_turn(shown, self.opener)
        if self.playing is None or turn != self.playing:
            return
        col, row = move
        size = len(xs)
        if not (0 <= col < size and 0 <= row < len(ys)):
            return
        if shown[row * size + col] != EMPTY:
            return
        guarded = _guard_move(shown, size, self.playing, (col, row))
        if guarded is None:
            return
        col, row = guarded
        if shown[row * size + col] != EMPTY:
            return
        origin_x, origin_y, width, height = self.region
        sx = round(origin_x + xs[col] * scale_x)
        sy = round(origin_y + ys[row] * scale_y)
        if not (origin_x <= sx < origin_x + width and origin_y <= sy < origin_y + height):
            return
        self.acted_board = snapshot
        self.seat.pending = row * size + col
        try:
            self._click((col, row), xs, ys, scale_x, scale_y)
        except pyautogui.FailSafeException:
            self.auto = False
            self.note = ""
            self._show_auto_state()
            self.status.set("Đã dừng tự động: chuột chạm góc màn hình.")
            return
        finally:
            self.acted_at = time.perf_counter()
            self._park_mouse()


def main():
    parser = argparse.ArgumentParser(description="Đọc bàn cờ caro trên màn hình và gợi ý nước đi")
    parser.add_argument(
        "--side",
        choices=("auto", "first", "second"),
        default="auto",
        help="Bên do tool điều khiển. Tự động tự nhận đi trước hay đi sau.",
    )
    parser.add_argument("--depth", type=int, default=6, help="Độ sâu tìm nước tối đa, từ 1 đến 8")
    args = parser.parse_args()
    print("Mở bàn cờ caro, rồi kéo một khung chứa cả bàn. Thừa viền cũng được.")
    print("Tool đọc quân và khoanh một nước. Phím F2 bật hoặc tắt tự click.")
    print("Tự động chỉ đi khi đến lượt mình, và chờ trong lúc đối thủ suy nghĩ.")
    print("Dừng khẩn: đưa chuột vào góc trên bên trái màn hình chính.")
    region = select_region()
    if region is None:
        print("Đã hủy.")
        return
    watcher = Watcher(region, args.side, max(1, min(8, args.depth)))
    watcher.panel.mainloop()


if __name__ == "__main__":
    main()
