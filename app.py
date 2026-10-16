"""
Đọc bàn cờ caro trên màn hình và gợi ý một nước.

Mở game bằng trình duyệt, rồi kéo một khung chứa cả bàn cờ.
Thừa viền trắng cũng được. Tool tìm các đường kẻ, đọc quân đen và quân trắng,
rồi khoanh nước nên đi. Phím T bật hoặc tắt tự click.

    python app.py
    python app.py --side black
    python app.py --side white
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

# Move choice follows gomokuAI. Every row, column and diagonal is scored by
# the 5-cell and 6-cell windows below (the same shapes for both colors).
# Search only looks at empty points within two steps of a stone. A five, or
# the single square that stops the other side's five, is played at once. An
# open four is played when the other side has none of the 5-cell threats.
# Anything else is negamax with alpha-beta, ordered by the pattern score of
# the lines through each point.

_INF = 10**15

_FIVE = {
    "11111": 1_000_000,
    "11110": 5_000,
    "01111": 5_000,
    "11011": 5_000,
    "10111": 5_000,
    "11101": 5_000,
}
_SIX = {
    "011110": 50_000,
    "011100": 5_000,
    "001110": 5_000,
    "011010": 500,
    "010110": 500,
    "001100": 100,
    "001010": 100,
    "010100": 100,
    "001000": 10,
    "000100": 10,
}

_AXES = ((1, 0), (0, 1), (1, 1), (1, -1))
_RAYS = ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (-1, -1), (1, -1), (-1, 1))
_SWAP = str.maketrans("12", "21")


def opponent(player: int) -> int:
    return WHITE if player == BLACK else BLACK


def side_to_move(board: list[int]) -> int | None:
    """Black starts. After that the side with fewer stones moves.

    A gap larger than one stone means the reading skipped or invented a stone.
    """
    black = sum(value == BLACK for value in board)
    white = sum(value == WHITE for value in board)
    if black == white:
        return BLACK
    if black == white + 1:
        return WHITE
    return None


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
    board: list[int], size: int, player: int, depth: int = 6, time_limit: float | None = 1.0
) -> tuple[int, int] | None:
    """Return the column and row to play, or None when the game is over or full.

    The first look is a five of our own, then the one square that blocks a five
    of theirs. An open four comes next, when they have no 5-cell pattern.
    The rest is alpha-beta, deeper and deeper until depth or time_limit.
    The first level always finishes, so there is always an answer.
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

    other = opponent(player)
    our_five = None
    their_five = None
    for y in range(size):
        for x in range(size):
            if board[y * size + x] != EMPTY:
                continue
            if our_five is None and _makes_five(board, size, x, y, player):
                our_five = (x, y)
            elif their_five is None and _makes_five(board, size, x, y, other):
                their_five = (x, y)
    if our_five is not None:
        return our_five
    if their_five is not None:
        return their_five
    if not _has_five_pattern(board, size, other):
        for y in range(size):
            for x in range(size):
                if board[y * size + x] != EMPTY:
                    continue
                if _has_neighbor(board, size, x, y) and _makes_open_four(board, size, x, y, player):
                    return x, y

    pos = _Board(board, size)
    deadline = time.perf_counter() + time_limit if time_limit else None
    depth = max(1, min(12, depth))
    best = None
    prev = None
    for level in range(1, depth + 1):
        try:
            score, move = _search(pos, player, level, -_INF, _INF, deadline if level > 1 else None)
        except _Timeout:
            break
        if move is None:
            break
        best = move
        # A finished five is already decisive. Once two deeper passes agree,
        # more depth only spends the time the next scan is waiting on.
        if abs(score) >= 500_000 or (move == prev and level >= 3):
            break
        prev = move
        if deadline is not None and time.perf_counter() > deadline:
            break
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
    return best % size, best // size


def _inside(size: int, x: int, y: int) -> bool:
    return 0 <= x < size and 0 <= y < size


def _window_score(text: str) -> int:
    """Score of one line written with 1 for the side being counted."""
    length = len(text)
    if length < 5:
        return 0
    if length == 5:
        return _FIVE.get(text, 0)
    score = 0
    for start in range(length - 4):
        score += _FIVE.get(text[start : start + 5], 0)
    for start in range(length - 5):
        score += _SIX.get(text[start : start + 6], 0)
    return score


def _line_scores(text: str) -> tuple[int, int]:
    """Black score and white score of one line."""
    return _window_score(text), _window_score(text.translate(_SWAP))


def _score_cells(board: list[int], cells: tuple[int, ...]) -> tuple[int, int]:
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


def _makes_open_four(board: list[int], size: int, x: int, y: int, player: int) -> bool:
    """True when playing here makes empty-stone-stone-stone-stone-empty."""
    for dx, dy in _AXES:
        count = 1
        ends = []
        for sign in (1, -1):
            nx = x + sign * dx
            ny = y + sign * dy
            while _inside(size, nx, ny) and board[ny * size + nx] == player:
                count += 1
                nx += sign * dx
                ny += sign * dy
            ends.append(_inside(size, nx, ny) and board[ny * size + nx] == EMPTY)
        if count == 4 and ends[0] and ends[1]:
            return True
    return False


def _has_neighbor(board: list[int], size: int, x: int, y: int) -> bool:
    """A stone lies one or two steps away along a row, column or diagonal."""
    _lines, _through, neigh = _geometry(size)
    index = y * size + x
    return any(board[other] for other in neigh[index])


def _has_five_pattern(board: list[int], size: int, player: int) -> bool:
    """True when player already has one of the 5-cell patterns on a line."""
    lines, _through, _neigh = _geometry(size)
    for cells in lines:
        text = "".join(str(board[index]) for index in cells)
        if player == WHITE:
            text = text.translate(_SWAP)
        if len(text) < 5:
            continue
        if len(text) == 5:
            if text in _FIVE:
                return True
            continue
        for start in range(len(text) - 4):
            if text[start : start + 5] in _FIVE:
                return True
    return False


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
        for cells in self.lines:
            black, white = _score_cells(self.board, cells)
            self.black.append(black)
            self.white.append(white)
        self.sum_black = sum(self.black)
        self.sum_white = sum(self.white)
        self.near = [0] * (size * size)
        for index, value in enumerate(self.board):
            if value:
                for other in self.neigh[index]:
                    self.near[other] += 1
        self.history: list[int] = []

    def value(self, player: int) -> int:
        """Pattern points of player minus pattern points of the other side."""
        if player == BLACK:
            return self.sum_black - self.sum_white
        return self.sum_white - self.sum_black

    def place(self, index: int, player: int) -> None:
        self.board[index] = player
        for number in self.through[index]:
            self.sum_black -= self.black[number]
            self.sum_white -= self.white[number]
            black, white = _score_cells(self.board, self.lines[number])
            self.black[number] = black
            self.white[number] = white
            self.sum_black += black
            self.sum_white += white
        for other in self.neigh[index]:
            self.near[other] += 1
        self.history.append(index)

    def undo(self) -> None:
        index = self.history.pop()
        self.board[index] = EMPTY
        for number in self.through[index]:
            self.sum_black -= self.black[number]
            self.sum_white -= self.white[number]
            black, white = _score_cells(self.board, self.lines[number])
            self.black[number] = black
            self.white[number] = white
            self.sum_black += black
            self.sum_white += white
        for other in self.neigh[index]:
            self.near[other] -= 1

    def candidates(self, player: int) -> list[int]:
        """Empty cells within two steps of a stone, strongest lines first.

        Both colors count, so a square that blocks the other side ranks with
        a square that builds our own line.
        """
        ours = self.black if player == BLACK else self.white
        theirs = self.white if player == BLACK else self.black
        found = [
            index
            for index, value in enumerate(self.board)
            if not value and self.near[index]
        ]
        found.sort(
            key=lambda index: (
                sum(ours[number] + theirs[number] for number in self.through[index]),
                self._closeness(index),
            ),
            reverse=True,
        )
        return found

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


def _search(
    pos: _Board,
    player: int,
    depth: int,
    alpha: int,
    beta: int,
    deadline: float | None,
) -> tuple[int, int | None]:
    if deadline is not None and time.perf_counter() > deadline:
        raise _Timeout
    if depth <= 0:
        return pos.value(player), None
    moves = pos.candidates(player)
    if not moves:
        return 0, None
    # The lines above already put blocks and builds first. Keeping the rest
    # would spend the whole time budget inside one depth and never go deeper.
    cap = 18 if depth <= 1 else (14 if depth == 2 else 10)
    if len(moves) > cap:
        moves = moves[:cap]
    other = opponent(player)
    best = -_INF
    best_move = moves[0]
    for index in moves:
        pos.place(index, player)
        try:
            score, _reply = _search(pos, other, depth - 1, -beta, -alpha, deadline)
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
    if any(abs(gap - period) > max(3, period * 0.25) for gap in gaps):
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
        cursor = start
        while len(coords) < 19:
            guess = cursor + sign * step
            if guess < 1 or guess >= len(score) - 1:
                break
            snapped = snap(guess)
            if score[snapped] < thresh or abs(snapped - guess) > 3:
                break
            if snapped in coords:
                break
            coords.append(snapped)
            cursor = snapped
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


def parse_board(image, xs=None, ys=None):
    """Return line columns, line rows, and one value per intersection.

    xs and ys, when given, are reused instead of searching for the grid again.
    The five star dots fix the grid when they are visible. Line search is the fallback.
    Returns None when no regular grid is visible.
    """
    if image.mode != "RGB":
        image = image.convert("RGB")
    pixels = image.load()
    width, height = image.size
    if xs is None or ys is None:
        found = _grid_from_stars(pixels, width, height)
        if found is None:
            xs = _lines(pixels, width, height, vertical=True)
            ys = _lines(pixels, width, height, vertical=False)
        else:
            xs, ys = found
    if not xs or not ys or len(xs) != len(ys):
        return None
    size = len(xs)
    if not 9 <= size <= 19:
        return None
    step = (xs[-1] - xs[0]) / (size - 1)
    if step < 8:
        return None
    board = []
    for y in ys:
        for x in xs:
            board.append(_stone(pixels, width, height, x, y, step))
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
        # A line hidden under stones leaves a one-step hole. The hole still
        # has some stroke beside the stones. An image border does not.
        skip = _snap(peaks, cursor + sign * 2 * period, period)
        if skip is not None and (skip - cursor) * sign > 0 and missed < 2:
            missing = cursor + sign * period
            faint = max(4, int(max(score) * 0.08)) if score else 4
            if 1 <= missing < length - 1 and _supported(score, missing, faint):
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


def _rgb_disk(pixels, width: int, height: int, x: int, y: int, radius: int) -> list[tuple[int, int, int]]:
    colors = []
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            if dx * dx + dy * dy > radius * radius:
                continue
            if abs(dx) <= 1 or abs(dy) <= 1:
                continue
            pixel = _at(pixels, width, height, x + dx, y + dy)
            if pixel is not None:
                colors.append(pixel[:3])
    return colors


def _stone(pixels, width: int, height: int, x: int, y: int, step: float) -> int:
    background = _background(pixels, width, height, x, y, step)
    if background is None:
        return EMPTY
    radius = max(4, int(step * 0.26))
    colors = _rgb_disk(pixels, width, height, x, y, radius)
    if len(colors) < 8:
        return EMPTY
    teal = sum(1 for red, green, blue in colors if _is_teal(red, green, blue)) / len(colors)
    ink = sum(1 for red, green, blue in colors if _is_ink(red, green, blue)) / len(colors)
    # Teal is the O on this board. It is darker than the page, so brightness
    # alone would call it black and glue both colors into one line of five.
    if teal > 0.35 and teal >= ink:
        return WHITE
    if ink > 0.45 and ink > teal:
        return BLACK
    disk = _disk(pixels, width, height, x, y, radius, skip_cross=True)
    if len(disk) < 8:
        return EMPTY
    disk.sort()
    median = disk[len(disk) // 2]
    dark = sum(value < background - 35 for value in disk) / len(disk)
    if dark > 0.45 and median < background - 28 and ink >= teal:
        return BLACK
    # White stones on a pale page are only a little brighter than the wood.
    lifted = sum(value >= background + 5 for value in disk) / len(disk)
    if lifted > 0.5 and median >= background + 5 and dark < 0.25:
        return WHITE
    # Same color as the page, but the stone still has a gray rim.
    if (
        _has_stone_ring(pixels, width, height, x, y, step, background)
        and median >= background - 12
        and dark < 0.35
    ):
        return WHITE
    return EMPTY


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


def _disk(pixels, width, height, x, y, radius, skip_cross: bool) -> list[int]:
    values = []
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            if dx * dx + dy * dy > radius * radius:
                continue
            if skip_cross and (abs(dx) <= 1 or abs(dy) <= 1):
                continue
            pixel = _at(pixels, width, height, x + dx, y + dy)
            if pixel is not None:
                values.append(_brightness(pixel))
    return values


def _has_stone_ring(pixels, width, height, x, y, step, background) -> bool:
    """True when a ring at the usual stone edge is darker than the page."""
    inner = max(3, int(step * 0.30))
    outer = max(inner + 2, int(step * 0.48))
    values = []
    for dy in range(-outer, outer + 1):
        for dx in range(-outer, outer + 1):
            dist = dx * dx + dy * dy
            if dist < inner * inner or dist > outer * outer:
                continue
            if abs(dx) <= 1 or abs(dy) <= 1:
                continue
            pixel = _at(pixels, width, height, x + dx, y + dy)
            if pixel is not None:
                values.append(_brightness(pixel))
    if len(values) < 8:
        return False
    darker = sum(value < background - 22 for value in values) / len(values)
    return darker > 0.16


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
        self.pending: tuple | None = None
        self.stable: tuple | None = None
        self.prev_raw: tuple | None = None
        self.grid_at = 0.0
        self.alt: tuple | None = None
        self.changed_at = time.perf_counter()
        self.acted_at = 0.0
        self.acted_board: tuple | None = None
        self.retries = 0
        self._lock = threading.Lock()
        self._gen = 0
        self._worker_key = None
        self._result_key = None
        self._result_move = None
        self.running = True
        self.auto = False
        self.t_down = False
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
        self.interval = 160 if self.quiet else 280

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
        for value, label in (("auto", "Tự động"), ("black", "Đen"), ("white", "Trắng")):
            tk.Radiobutton(options, text=label, variable=self.side_var, value=value, command=self._invalidate).pack(
                side="left"
            )
        tk.Checkbutton(options, text="Nghĩ kỹ hơn", variable=self.deep, command=self._invalidate).pack(side="left", padx=8)
        tk.Checkbutton(options, text="Hiện quân đã đọc", variable=self.show_stones, command=self._redraw).pack(side="left")

        buttons = tk.Frame(self.panel)
        buttons.pack(side="bottom", pady=(0, 4))
        tk.Button(buttons, text="Đọc lại lưới", command=self.reset_grid).pack(side="left", padx=4)
        self.auto_button = tk.Button(buttons, command=self.toggle_auto, width=18)
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
            self.auto_text.set("TỰ ĐỘNG: BẬT  (bấm T để tắt)")
            self.auto_button.configure(text="Tắt tự động (T)")
        else:
            self.auto_text.set("TỰ ĐỘNG: TẮT  (bấm T để bật)")
            self.auto_button.configure(text="Bật tự động (T)")

    def _invalidate(self):
        with self._lock:
            self._gen += 1
            self._result_key = None

    def _redraw(self):
        self.last_marks = None

    def poll_keys(self):
        if not self.running:
            return
        state = ctypes.windll.user32.GetAsyncKeyState
        down = bool(state(0x54) & 0x8000)
        modifier = any(state(key) & 0x8000 for key in (0x11, 0x12, 0x5B, 0x5C))
        if down and not self.t_down and not modifier:
            self.toggle_auto()
        self.t_down = down
        self.panel.after(40, self.poll_keys)

    def _t_pressed(self) -> bool:
        return bool(ctypes.windll.user32.GetAsyncKeyState(0x54) & 0x8000)

    def reset_grid(self):
        self.xs = None
        self.ys = None
        self.pending = None
        self.stable = None
        self.prev_raw = None
        self.grid_at = 0.0
        self.alt = None
        self.acted_board = None
        self.retries = 0
        self.last_marks = None
        self._invalidate()
        self.status.set("Đang tìm lại lưới")

    def toggle_auto(self):
        if pyautogui is None:
            self.status.set("Chưa cài pyautogui. Chạy: pip install pyautogui")
            return
        self.auto = not self.auto
        self.acted_board = None
        self.acted_at = 0.0
        self.retries = 0
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

    def _sample(self, image):
        """Read stones on the locked grid. Re-find the lines about once a second.

        A fresh find that is only a pixel off the lock is ignored, so the click
        point and the stone colors do not jitter. A genuinely different grid
        has to show up twice before it replaces the lock.
        """
        now = time.perf_counter()
        if self.xs is not None and now - self.grid_at < 1.0:
            return parse_board(image, self.xs, self.ys)
        fresh = parse_board(image)
        self.grid_at = now
        if fresh is None:
            if self.xs is None:
                return None
            return parse_board(image, self.xs, self.ys)
        nxs, nys, _board = fresh
        if self.xs is None:
            self.xs, self.ys = list(nxs), list(nys)
            return fresh
        if _grids_close(nxs, self.xs) and _grids_close(nys, self.ys):
            self.alt = None
            return parse_board(image, self.xs, self.ys)
        if self.alt is not None and _grids_close(nxs, self.alt[0]) and _grids_close(nys, self.alt[1]):
            self.xs, self.ys = list(nxs), list(nys)
            self.alt = None
            self.stable = None
            self.prev_raw = None
            self.pending = None
            return fresh
        self.alt = (list(nxs), list(nys))
        return parse_board(image, self.xs, self.ys)

    def _read_and_mark(self):
        if not self.quiet:
            self.overlay.withdraw()
            self.panel.update()
            time.sleep(0.03)
        x, y, width, height = self.region
        image = grab((x, y, x + width, y + height))
        parsed = self._sample(image)
        if parsed is None:
            self.status.set("Chưa thấy lưới. Kéo khung rộng hơn, phủ hết bàn cờ, rồi bấm Đọc lại lưới.")
            self._show_overlay()
            return
        xs, ys, board = parsed
        size = len(xs)
        self.interval = 80 if self.quiet else 140
        raw = tuple(board)
        agreed = self.prev_raw is not None and raw == self.prev_raw
        now = time.perf_counter()
        if agreed and raw != self.stable:
            self.stable = raw
            self.changed_at = now
        self.prev_raw = raw
        settled = self.stable is not None and raw == self.stable
        shown = list(self.stable if self.stable is not None else raw)

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
        self.status.set(f"{size}×{size}. {advice}")
        current = self.board.get("1.0", "end-1c")
        if current != picture:
            self.board.delete("1.0", "end")
            self.board.insert("end", picture)
        self._auto_play(move, advice, settled, shown, xs, ys, scale_x, scale_y)

    def _think(self, board: list[int], size: int, player: int, depth: int, budget: float, key, gen: int) -> None:
        try:
            move = choose_move(board, size, player, depth, budget)
        except Exception:
            move = None
        with self._lock:
            if self._gen == gen and self._worker_key == key:
                self._result_key = key
                self._result_move = move
            if self._worker_key == key:
                self._worker_key = None

    def _advise(self, board: list[int], size: int, settled: bool) -> tuple[tuple[int, int] | None, str]:
        done = winner(board, size)
        if done == BLACK:
            return None, "Đen đã có năm quân."
        if done == WHITE:
            return None, "Trắng đã có năm quân."
        turn = side_to_move(board)
        choice = self.side_var.get()
        black = sum(value == BLACK for value in board)
        white = sum(value == WHITE for value in board)
        counts = f"Đen {black}, trắng {white}."
        if turn is None and choice == "auto":
            return None, f"{counts} Số quân lệch, hãy chọn bên Đen hoặc Trắng."
        if choice == "auto":
            player = turn
        else:
            player = BLACK if choice == "black" else WHITE
            if turn is not None and turn != player:
                name = "đen" if turn == BLACK else "trắng"
                return None, f"{counts} Đến lượt {name}."
            if not any(board) and player == WHITE:
                return None, "Bàn trống. Đen đi trước."
        if self.deep.get():
            depth, budget = self.depth + 2, 0.7
        else:
            depth, budget = self.depth, 0.25
        key = (tuple(board), player, depth, budget)
        with self._lock:
            if self._result_key == key:
                move = self._result_move
            else:
                move = None
                if settled and self._worker_key is None:
                    self._worker_key = key
                    threading.Thread(
                        target=self._think,
                        args=(list(board), size, player, depth, budget, key, self._gen),
                        daemon=True,
                    ).start()
        if move is None:
            if not settled:
                return None, f"{counts} Đang đọc bàn…"
            return None, f"{counts} Đang tính nước…"
        name = "Đen" if player == BLACK else "Trắng"
        return move, f"{counts} Lượt {name}. Gợi ý hàng {move[1] + 1}, cột {move[0] + 1}."

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
        time.sleep(0.02)
        pyautogui.mouseDown(sx, sy, button="left")
        time.sleep(0.02)
        pyautogui.mouseUp(sx, sy, button="left")

    def _auto_play(self, move, advice, settled, shown, xs, ys, scale_x, scale_y):
        if not self.auto or not self.running or pyautogui is None or not settled or move is None:
            return
        if advice.startswith("Đen đã") or advice.startswith("Trắng đã"):
            return
        now = time.perf_counter()
        # The new stone has to sit still briefly, so a placing animation is not read as the stone.
        if now - self.changed_at < 0.10:
            return
        snapshot = tuple(shown)
        if snapshot == self.acted_board:
            if now - self.acted_at < 0.45:
                return
            if self.retries >= 2:
                self.auto = False
                self._show_auto_state()
                self.status.set("Bàn không đổi sau khi click. Đã tắt tự động.")
                return
            self.retries += 1
        else:
            if now - self.acted_at < 0.16:
                return
            self.retries = 0
        if self._t_pressed():
            return
        self.acted_board = snapshot
        try:
            self._click(move, xs, ys, scale_x, scale_y)
        except pyautogui.FailSafeException:
            self.auto = False
            self._show_auto_state()
            self.status.set("Đã dừng tự động: chuột chạm góc màn hình.")
            return
        finally:
            self.acted_at = time.perf_counter()
            self._park_mouse()


def main():
    parser = argparse.ArgumentParser(description="Đọc bàn cờ caro trên màn hình và gợi ý nước đi")
    parser.add_argument("--side", choices=("auto", "black", "white"), default="auto", help="Bên do tool điều khiển")
    parser.add_argument("--depth", type=int, default=6, help="Độ sâu tìm nước tối đa, từ 1 đến 8")
    args = parser.parse_args()
    print("Mở bàn cờ caro, rồi kéo một khung chứa cả bàn. Thừa viền cũng được.")
    print("Tool đọc quân và khoanh một nước. Phím T bật hoặc tắt tự click.")
    print("Dừng khẩn: đưa chuột vào góc trên bên trái màn hình chính.")
    region = select_region()
    if region is None:
        print("Đã hủy.")
        return
    watcher = Watcher(region, args.side, max(1, min(8, args.depth)))
    watcher.panel.mainloop()


if __name__ == "__main__":
    main()
