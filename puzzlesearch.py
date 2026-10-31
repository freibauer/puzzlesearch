#!/usr/bin/env python3
"""Search chess puzzles in a PGN file with Stockfish.

For every game all positions are scanned quickly. Moves that throw away a
large part of the winning chances (blunders) are candidates. Each candidate is
verified with a deep multi-PV search: every move of the solver must be the
only good move, i.e. the second best move must be at least --min-gap
centipawns worse. Two different mating moves always count as ambiguous.

Output:
  * a CSV file with one puzzle per row
  * a PGN file with the complete game of every puzzle, in the same order
"""

import argparse
import csv
import io
import math
import multiprocessing as mp
import os
import shutil
import sys
import time
from dataclasses import dataclass

import chess
import chess.engine
import chess.pgn

MATE_SCORE = 100000
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

CSV_FIELDS = [
    "PuzzleId", "GameNo", "Event", "Site", "Date", "Round", "White", "Black",
    "Result", "GameId", "BlunderPly", "FEN", "Moves", "PuzzleFEN", "SideToMove",
    "Solution", "SolverMoves", "EvalBefore", "EvalAfter", "EvalFinal",
    "MinGap", "Themes",
]


@dataclass
class Config:
    engine: str
    hash_mb: int
    scan_limit: chess.engine.Limit
    prefilter_limit: chess.engine.Limit
    verify_limit: chess.engine.Limit
    confirm_limit: "chess.engine.Limit | None"
    min_gap: int
    min_eval: int
    min_jump: float
    max_solver_moves: int
    max_per_game: int
    prefilter: bool
    keep_recaptures: bool
    keep_free_captures: bool
    trim: bool
    min_material: int
    min_ply: int
    max_ply: int
    max_imbalance: "int | None"


# ---------------------------------------------------------------- helpers

def win_chance(cp):
    """Winning chance in [0, 1] for a centipawn score (lichess formula)."""
    cp = max(-2000, min(2000, cp))
    return 1 / (1 + math.exp(-0.00368208 * cp))


def cp_of(score):
    return score.score(mate_score=MATE_SCORE)


def is_solver_mate(score):
    return score.is_mate() and score.mate() > 0


def fmt_score(score):
    if score.is_mate():
        return "#%d" % score.mate()
    return "%+d" % score.score()


def gap_of(best, second):
    """Centipawn gap between best and second best move, None if ambiguous.

    second is None when there is only one legal move.
    """
    if second is None:
        return MATE_SCORE
    if is_solver_mate(best) and is_solver_mate(second):
        return None
    gap = cp_of(best) - cp_of(second)
    return gap


PIECE_VALUES = {chess.PAWN: 100, chess.KNIGHT: 300, chess.BISHOP: 300, chess.ROOK: 500,
                chess.QUEEN: 900, chess.KING: 0}


def material(board, color):
    """Material balance in cp from color's point of view."""
    total = 0
    for pt, v in PIECE_VALUES.items():
        total += v * (len(board.pieces(pt, color)) - len(board.pieces(pt, not color)))
    return total


def _captured_value(board, move):
    if board.is_en_passant(move):
        return PIECE_VALUES[chess.PAWN]
    piece = board.piece_at(move.to_square)
    return PIECE_VALUES[piece.piece_type] if piece else 0


def _see_square(board, square):
    """Best exchange gain for the side to move by recapturing on square."""
    captures = [m for m in board.legal_moves if m.to_square == square]
    if not captures:
        return 0
    move = min(captures, key=lambda m: PIECE_VALUES[board.piece_type_at(m.from_square)])
    value = _captured_value(board, move)
    board.push(move)
    gain = value - _see_square(board, square)
    board.pop()
    return max(0, gain)


def see(board, move):
    """Static exchange evaluation of a capture (cp, for the moving side)."""
    board = board.copy()
    value = _captured_value(board, move)
    board.push(move)
    return value - _see_square(board, move.to_square)


def material_steps(board, moves):
    """Solver's material after each solver move.

    Counted after the opponent's reply; after the last move the opponent's
    best recapture (static exchange) is taken into account.
    """
    board = board.copy(stack=False)
    solver = board.turn
    steps = []
    for k, move in enumerate(moves):
        board.push(move)
        if k % 2 == 1:
            steps.append(material(board, solver))
        elif k == len(moves) - 1:
            steps.append(material(board, solver) - _see_square(board, move.to_square))
    return steps


def material_gain(board, moves):
    """Material the solution wins (cp)."""
    return material_steps(board, moves)[-1] - material(board, board.turn)


def trim_line(board, moves):
    """Cut the solution after the solver move that realizes the material gain.

    Later moves only consolidate the advantage and are not part of the puzzle.
    """
    steps = material_steps(board, moves)
    best = max(steps)
    if best <= material(board, board.turn):
        return moves, len(steps) - 1
    k = steps.index(best)
    return moves[:2 * k + 1], k


def is_free_capture(board, moves, gain):
    """True if the first move simply takes material that was left hanging.

    That is the case when the first move is a capture whose static exchange
    gain already covers (almost) all material the solution wins.
    """
    first = moves[0]
    if not board.is_capture(first):
        return False
    first_gain = see(board, first)
    return first_gain > 0 and first_gain >= gain - PIECE_VALUES[chess.PAWN]


def san_line(board, moves):
    """'23. Qxh7+ Kxh7 24. Rh3#' style notation of moves starting at board."""
    return board.variation_san(moves)


def phase_theme(board, ply):
    pieces = sum(len(board.pieces(pt, c)) for pt in (chess.KNIGHT, chess.BISHOP, chess.ROOK, chess.QUEEN)
                 for c in chess.COLORS)
    if pieces <= 6:
        return "endgame"
    if ply <= 24:
        return "opening"
    return "middlegame"


# ---------------------------------------------------------------- worker

_engine = None
_cfg = None


def _init_worker(cfg):
    global _engine, _cfg
    _cfg = cfg
    _engine = chess.engine.SimpleEngine.popen_uci(cfg.engine)
    _engine.configure({"Threads": 1, "Hash": cfg.hash_mb})
    import atexit
    atexit.register(_engine.quit)


def _analyse(board, limit, multipv=None, key=None):
    return _engine.analyse(board, limit, multipv=multipv, game=key)


def _scan_score(board, key):
    """White POV score of a position (quick search)."""
    if board.is_checkmate():
        return chess.engine.PovScore(chess.engine.Mate(0), board.turn).white()
    if board.is_game_over(claim_draw=False):
        return chess.engine.Cp(0)
    return _analyse(board, _cfg.scan_limit, key=key)["score"].white()


def _build_line(board, key):
    """Find the unique solution starting at board (solver to move).

    Returns (moves, scores, gaps) or None; scores and gaps per solver move.
    """
    cfg = _cfg
    board = board.copy(stack=False)
    solver = board.turn
    moves, scores, gaps = [], [], []
    mate_puzzle = False

    for k in range(cfg.max_solver_moves):
        legal = board.legal_moves.count()
        if k == 0 and legal == 1:
            return None
        infos = _analyse(board, cfg.verify_limit, multipv=2, key=key)
        best = infos[0]["score"].pov(solver)
        second = infos[1]["score"].pov(solver) if len(infos) > 1 else None

        if k == 0:
            mate_puzzle = is_solver_mate(best)
            if not mate_puzzle and cp_of(best) < cfg.min_eval:
                return None
        elif mate_puzzle and not is_solver_mate(best):
            return None
        elif not mate_puzzle and cp_of(best) < cfg.min_eval:
            break

        gap = gap_of(best, second)
        if gap is None or gap < cfg.min_gap:
            if k == 0 or mate_puzzle:
                return None
            break

        gaps.append(gap)
        scores.append(best)
        move = infos[0]["pv"][0]
        moves.append(move)
        board.push(move)
        if board.is_checkmate():
            break
        if board.is_game_over(claim_draw=False):
            return None

        reply = _analyse(board, cfg.verify_limit, key=key)["pv"][0]
        moves.append(reply)
        board.push(reply)
        if board.is_game_over(claim_draw=False):
            break
    else:
        if mate_puzzle:
            return None  # mate longer than max_solver_moves

    if len(moves) % 2 == 0:
        moves.pop()  # line must end with a solver move
    if not moves:
        return None
    return moves, scores, gaps


def _confirm_line(board, moves, key):
    """Re-check every solver move with the (longer) confirm search.

    The chosen move must still be the best move and the second best move must
    still be at least min_gap worse. Returns the smallest gap or None.
    """
    cfg = _cfg
    board = board.copy(stack=False)
    solver = board.turn
    gaps = []
    for k, move in enumerate(moves):
        if k % 2 == 0 and board.legal_moves.count() > 1:
            infos = _analyse(board, cfg.confirm_limit, multipv=2, key=key)
            if infos[0]["pv"][0] != move:
                return None
            best = infos[0]["score"].pov(solver)
            second = infos[1]["score"].pov(solver) if len(infos) > 1 else None
            gap = gap_of(best, second)
            if gap is None or gap < cfg.min_gap:
                return None
            gaps.append(gap)
        board.push(move)
    return min(gaps) if gaps else MATE_SCORE


def _ends_in_mate(board, moves):
    b = board.copy(stack=False)
    for m in moves:
        b.push(m)
    return b.is_checkmate()


def process_game(item):
    """Analyse one game. Returns (game_no, [puzzle dicts])."""
    game_no, pgn_text = item
    cfg = _cfg
    try:
        game = chess.pgn.read_game(io.StringIO(pgn_text))
    except Exception:
        return game_no, []
    if game is None or game.errors:
        return game_no, []
    if game.headers.get("Variant", "Standard").lower() not in ("standard", "chess"):
        return game_no, []

    key = object()
    nodes = list(game.mainline())
    if cfg.max_ply:
        nodes = nodes[:cfg.max_ply]  # later blunders are not wanted, no need to scan them
    if len(nodes) < 2:
        return game_no, []

    # quick scan: White POV scores of all positions
    board = game.board()
    scan = [_scan_score(board, key)]
    for node in nodes:
        board.push(node.move)
        scan.append(_scan_score(board, key))

    puzzles = []
    board = game.board()
    for i, node in enumerate(nodes):
        before = board.copy(stack=False)
        board.push(node.move)
        if i + 1 < cfg.min_ply or board.is_game_over(claim_draw=False):
            continue
        if cfg.max_imbalance is not None and abs(material(before, chess.WHITE)) > cfg.max_imbalance:
            continue
        solver = board.turn
        sign = 1 if solver == chess.WHITE else -1
        cp_before = sign * cp_of(scan[i])
        cp_after = sign * cp_of(scan[i + 1])
        if cp_after < cfg.min_eval or win_chance(cp_after) - win_chance(cp_before) < cfg.min_jump:
            continue

        # cheap rejection of obviously ambiguous positions
        if cfg.prefilter and board.legal_moves.count() > 1:
            infos = _analyse(board, cfg.prefilter_limit, multipv=2, key=key)
            if len(infos) > 1:
                g = gap_of(infos[0]["score"].pov(solver), infos[1]["score"].pov(solver))
                if g is None or g < cfg.min_gap / 2:
                    continue

        # deep confirmation of the blunder
        deep_before = _analyse(before, cfg.verify_limit, key=key)["score"].pov(solver)

        line = _build_line(board, key)
        if line is None:
            continue
        moves, scores, gaps = line
        first = scores[0]
        if win_chance(cp_of(first)) - win_chance(cp_of(deep_before)) < cfg.min_jump:
            continue
        mate = is_solver_mate(first)
        if mate and not _ends_in_mate(board, moves):
            continue
        if not mate:
            gain = material_gain(board, moves)
            if gain < cfg.min_material:
                continue
            if cfg.trim:
                moves, k = trim_line(board, moves)
                scores, gaps = scores[:k + 1], gaps[:k + 1]
            if not cfg.keep_free_captures and is_free_capture(board, moves, gain):
                continue
        last, min_gap = scores[-1], min(gaps)
        solver_moves = (len(moves) + 1) // 2
        if (not cfg.keep_recaptures and not mate and solver_moves == 1
                and before.is_capture(node.move) and moves[0].to_square == node.move.to_square):
            continue
        if cfg.confirm_limit is not None:
            confirmed = _confirm_line(board, moves, key)
            if confirmed is None:
                continue
            min_gap = min(min_gap, confirmed)

        puzzles.append(_make_puzzle(game, game_no, i, before, board, node, moves,
                                    deep_before, first, last, min_gap, mate, solver_moves))
        if cfg.max_per_game and len(puzzles) >= cfg.max_per_game:
            break

    return game_no, puzzles


def _make_puzzle(game, game_no, i, before, board, node, moves, deep_before, first, last,
                 min_gap, mate, solver_moves):
    h = game.headers
    pid = "%05d-%03d" % (game_no, i + 1)

    themes = []
    if mate:
        themes += ["mate", "mateIn%d" % solver_moves]
    else:
        themes.append("crushing" if cp_of(last) >= 600 else "advantage")
    themes.append({1: "oneMove", 2: "short", 3: "long"}.get(solver_moves, "veryLong"))
    themes.append(phase_theme(board, i + 1))

    solution = san_line(board, moves)

    # annotated copy of the complete game for the PGN output
    out = chess.pgn.read_game(io.StringIO(str(game)))
    out.headers["PuzzleId"] = pid
    out.headers["PuzzleFEN"] = board.fen()
    blunder_node = list(out.mainline())[i]
    blunder_node.nags.add(chess.pgn.NAG_BLUNDER)
    note = "Puzzle %s: %s (%s)" % (pid, solution, fmt_score(first))
    blunder_node.comment = (blunder_node.comment + " " + note).strip()

    row = {
        "PuzzleId": pid,
        "GameNo": game_no,
        "Event": h.get("Event", ""),
        "Site": h.get("Site", ""),
        "Date": h.get("Date", ""),
        "Round": h.get("Round", ""),
        "White": h.get("White", ""),
        "Black": h.get("Black", ""),
        "Result": h.get("Result", ""),
        "GameId": h.get("GameId", ""),
        "BlunderPly": i + 1,
        "FEN": before.fen(),
        "Moves": " ".join(m.uci() for m in [node.move] + moves),
        "PuzzleFEN": board.fen(),
        "SideToMove": "white" if board.turn == chess.WHITE else "black",
        "Solution": solution,
        "SolverMoves": solver_moves,
        "EvalBefore": fmt_score(deep_before),
        "EvalAfter": fmt_score(first),
        "EvalFinal": fmt_score(last),
        "MinGap": "mate" if min_gap >= MATE_SCORE else min_gap,
        "Themes": " ".join(themes),
    }
    return {"row": row, "pgn": str(out)}


# ---------------------------------------------------------------- main

def read_games(path, start, limit):
    """Yield (game_no, pgn_text) for games start..start+limit-1 (1-based)."""
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        no = 0
        while True:
            if no + 1 < start:
                if not chess.pgn.skip_game(f):
                    return
                no += 1
                continue
            game = chess.pgn.read_game(f)
            if game is None:
                return
            no += 1
            yield no, str(game)
            if limit and no >= start + limit - 1:
                return


def count_games(path):
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        return sum(1 for line in f if line.startswith("[Event "))


def last_game_no(csv_path):
    last = 0
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            last = max(last, int(row["GameNo"]))
    return last


class SimilarFilter:
    """Remembers puzzles and recognizes similar ones.

    Two puzzles are similar when the same side starts with the same move and
    either finishes with the same move (same motif, e.g. Qa4+ ... Qxb4), or
    plays the same second move, or the positions differ on at most `squares`
    squares.
    """

    def __init__(self, squares):
        self.squares = squares
        self.seen = {}  # (turn, first move) -> [(pieces, moves)]

    def is_new(self, row):
        """True (and remember the puzzle) if no similar puzzle was seen yet."""
        board = chess.Board(row["PuzzleFEN"])
        moves = row["Moves"].split()[1:]
        pieces = board.piece_map()
        bucket = self.seen.setdefault((board.turn, moves[0]), [])
        for other_pieces, other_moves in bucket:
            if (other_moves[-1] == moves[-1]
                    or (len(moves) > 2 and other_moves[2:3] == moves[2:3])
                    or self._diff(pieces, other_pieces) <= self.squares):
                return False
        bucket.append((pieces, moves))
        return True

    @staticmethod
    def _diff(a, b):
        return sum(1 for sq in set(a) | set(b) if a.get(sq) != b.get(sq))


def dedup_files(csv_path, pgn_path, squares, delimiter):
    """Remove similar puzzles from existing output files (no engine needed)."""
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f, delimiter=delimiter))
    with open(pgn_path, encoding="utf-8") as f:
        games = []
        while True:
            game = chess.pgn.read_game(f)
            if game is None:
                break
            games.append(game)
    if [g.headers.get("PuzzleId") for g in games] != [r["PuzzleId"] for r in rows]:
        sys.exit("%s and %s do not match" % (csv_path, pgn_path))

    sim = SimilarFilter(squares)
    keep = [i for i, row in enumerate(rows) if sim.is_new(row)]
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, delimiter=delimiter)
        writer.writeheader()
        for i in keep:
            writer.writerow(rows[i])
    with open(pgn_path, "w", encoding="utf-8") as f:
        for i in keep:
            f.write(str(games[i]) + "\n\n")
    print("%d of %d puzzles kept, %d similar removed" % (len(keep), len(rows), len(rows) - len(keep)),
          file=sys.stderr)


def find_engine(path):
    if path:
        return path
    local = os.path.join(SCRIPT_DIR, "engine", "stockfish")
    if os.path.exists(local):
        return local
    found = shutil.which("stockfish")
    if found:
        return found
    sys.exit("Stockfish not found. Run ./setup-stockfish.sh or pass --engine PATH.")


def make_limit(args, stage, factor=1):
    """Engine limit of a stage: time and/or depth if given, otherwise nodes."""
    t = getattr(args, stage + "_time")
    d = getattr(args, stage + "_depth")
    if t is None and d is None:
        return chess.engine.Limit(nodes=getattr(args, stage + "_nodes") * factor)
    return chess.engine.Limit(time=t * factor if t else None, depth=d)


def describe(limit):
    if limit is None:
        return "off"
    parts = []
    if limit.time:
        parts.append("%gs" % limit.time)
    if limit.depth:
        parts.append("depth %d" % limit.depth)
    if limit.nodes:
        parts.append("%d nodes" % limit.nodes)
    return ", ".join(parts)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("pgn", help="input PGN file")
    p.add_argument("-o", "--out", default=None, help="output basename (default: <pgn>-puzzles)")
    p.add_argument("--engine", default=None, help="path to Stockfish (default: engine/stockfish or PATH)")
    p.add_argument("-j", "--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1),
                   help="parallel engine processes, one thread each (default: CPUs-1)")
    p.add_argument("--hash", type=int, default=128, help="hash per engine in MB (default: 128)")
    # Search limits per stage. Time (seconds) or depth replace the node count when given.
    for stage, nodes, text in (("scan", 40000, "quick scan of every game position"),
                               ("verify", 1500000, "multi-PV search building the solution"),
                               ("confirm", 5000000, "final re-check of every solver move")):
        g = p.add_argument_group("%s search (%s)" % (stage, text))
        g.add_argument("--%s-nodes" % stage, type=int, default=nodes, help="nodes (default: %d)" % nodes)
        g.add_argument("--%s-time" % stage, type=float, default=None, help="seconds per position")
        g.add_argument("--%s-depth" % stage, type=int, default=None, help="search depth")
    p.add_argument("--no-confirm", action="store_true", help="skip the final confirm search")
    p.add_argument("--min-gap", type=int, default=100,
                   help="second best move must be at least this many cp worse (default: 100)")
    p.add_argument("--min-eval", type=int, default=200,
                   help="solver must be at least this many cp ahead after the blunder (default: 200)")
    p.add_argument("--min-jump", type=float, default=0.3,
                   help="minimum gain of winning chance (0..1) caused by the blunder (default: 0.3)")
    p.add_argument("--max-solver-moves", type=int, default=8, help="maximum solver moves in a solution")
    p.add_argument("--max-per-game", type=int, default=0, help="maximum puzzles per game (0 = no limit)")
    p.add_argument("--min-ply", type=int, default=1, help="ignore blunders before this ply")
    p.add_argument("--max-move", type=int, default=0,
                   help="only blunders up to this move number, e.g. 12 = opening only (0 = no limit)")
    p.add_argument("--max-imbalance", type=int, default=None,
                   help="only puzzles whose start position (before the blunder) has at most this "
                        "material difference in cp, 0 = balanced material (default: no limit)")
    p.add_argument("--no-prefilter", action="store_true", help="disable the cheap ambiguity pre-filter")
    p.add_argument("--keep-recaptures", action="store_true",
                   help="keep one-move puzzles that are just a recapture")
    p.add_argument("--keep-free-captures", action="store_true",
                   help="keep puzzles whose first move just takes a hanging piece")
    p.add_argument("--min-material", type=int, default=200,
                   help="non-mate solutions must win at least this much material in cp (default: 200)")
    p.add_argument("--no-trim", action="store_true",
                   help="do not cut the solution after the move that wins the material")
    p.add_argument("--keep-similar", action="store_true", help="keep similar puzzles")
    p.add_argument("--similar-squares", type=int, default=4,
                   help="puzzles with the same first move whose positions differ on at most this "
                        "many squares are similar (default: 4)")
    p.add_argument("--dedup", action="store_true",
                   help="only remove similar puzzles from existing output files, no analysis")
    p.add_argument("--start", type=int, default=1, help="first game number to analyse (1-based)")
    p.add_argument("--limit", type=int, default=0, help="number of games to analyse (0 = all)")
    p.add_argument("--resume", action="store_true", help="continue an interrupted run (appends output)")
    p.add_argument("--delimiter", default=",", help="CSV delimiter (default: ',')")
    args = p.parse_args()

    base = args.out or os.path.splitext(args.pgn)[0] + "-puzzles"
    csv_path, pgn_path = base + ".csv", base + ".pgn"

    if args.dedup:
        dedup_files(csv_path, pgn_path, args.similar_squares, args.delimiter)
        return

    cfg = Config(
        engine=find_engine(args.engine), hash_mb=args.hash,
        scan_limit=make_limit(args, "scan"), prefilter_limit=make_limit(args, "scan", 4),
        verify_limit=make_limit(args, "verify"),
        confirm_limit=None if args.no_confirm else make_limit(args, "confirm"),
        min_gap=args.min_gap, min_eval=args.min_eval,
        min_jump=args.min_jump, max_solver_moves=args.max_solver_moves,
        max_per_game=args.max_per_game, prefilter=not args.no_prefilter,
        keep_recaptures=args.keep_recaptures, keep_free_captures=args.keep_free_captures,
        trim=not args.no_trim, min_material=args.min_material, min_ply=args.min_ply,
        max_ply=2 * args.max_move, max_imbalance=args.max_imbalance,
    )

    sim = None if args.keep_similar else SimilarFilter(args.similar_squares)
    start = args.start
    append = False
    if args.resume and os.path.exists(csv_path):
        if sim:
            with open(csv_path, encoding="utf-8-sig", newline="") as f:
                for row in csv.DictReader(f, delimiter=args.delimiter):
                    sim.is_new(row)
        done = last_game_no(csv_path)
        start = max(start, done + 1)
        append = True
        print("Resuming after game %d" % done, file=sys.stderr)

    total = count_games(args.pgn)
    end = min(total, start + args.limit - 1) if args.limit else total
    todo = max(0, end - start + 1)
    print("%d games in file, analysing %d..%d with %d workers" % (total, start, end, args.workers),
          file=sys.stderr)
    print("scan: %s | verify: %s | confirm: %s | min gap %d cp"
          % (describe(cfg.scan_limit), describe(cfg.verify_limit), describe(cfg.confirm_limit),
             cfg.min_gap), file=sys.stderr)

    csv_file = open(csv_path, "a" if append else "w", encoding="utf-8" if append else "utf-8-sig",
                    newline="")
    pgn_file = open(pgn_path, "a" if append else "w", encoding="utf-8")
    writer = csv.DictWriter(csv_file, fieldnames=CSV_FIELDS, delimiter=args.delimiter)
    if not append:
        writer.writeheader()

    found = 0
    t0 = time.time()
    with mp.Pool(args.workers, initializer=_init_worker, initargs=(cfg,)) as pool:
        try:
            # imap keeps the input order, so CSV and PGN stay in game order
            for n, (game_no, puzzles) in enumerate(
                    pool.imap(process_game, read_games(args.pgn, start, args.limit)), 1):
                for pz in puzzles:
                    if sim and not sim.is_new(pz["row"]):
                        continue
                    writer.writerow(pz["row"])
                    pgn_file.write(pz["pgn"] + "\n\n")
                    found += 1
                if puzzles:
                    csv_file.flush()
                    pgn_file.flush()
                if n % 20 == 0 or n == todo:
                    el = time.time() - t0
                    eta = el / n * (todo - n)
                    print("\r%d/%d games, %d puzzles, %.1f games/s, ETA %dh%02dm   "
                          % (n, todo, found, n / el, eta // 3600, eta % 3600 // 60),
                          end="", file=sys.stderr, flush=True)
        except KeyboardInterrupt:
            pool.terminate()
            print("\nInterrupted - continue with --resume", file=sys.stderr)
    csv_file.close()
    pgn_file.close()
    print("\n%d puzzles written to %s and %s" % (found, csv_path, pgn_path), file=sys.stderr)


if __name__ == "__main__":
    main()
