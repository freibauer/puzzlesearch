# puzzlesearch

Finds chess puzzles in a PGN file with Stockfish and writes

* `<name>-puzzles.csv` – one puzzle per row
* `<name>-puzzles.pgn` – the complete game of each puzzle, **in the same order as the CSV**
  (headers `PuzzleId` and `PuzzleFEN` are added, the blunder is marked `??` with the solution as comment)

## Setup

```sh
pip install -r requirements.txt
./setup-stockfish.sh            # downloads the official Stockfish binary to engine/stockfish
```

## Usage

```sh
python3 puzzlesearch.py ../angenommenesdamengambit.pgn                # all games, all CPUs
python3 puzzlesearch.py ../angenommenesdamengambit.pgn --limit 50     # quick test
python3 puzzlesearch.py ../angenommenesdamengambit.pgn --resume       # continue after Ctrl-C

# only opening puzzles (blunder up to move 12) from positions with balanced material
python3 puzzlesearch.py ../angenommenesdamengambit.pgn --max-move 12 --max-imbalance 0

# more thinking time: seconds per position for each stage
python3 puzzlesearch.py ../angenommenesdamengambit.pgn --verify-time 2 --confirm-time 10
```

### Search time

Each of the three stages can be limited by nodes (default), seconds or depth:

| Stage | Purpose | Default | Options |
|---|---|---|---|
| scan | quick evaluation of every game position | 40000 nodes | `--scan-nodes/-time/-depth` |
| verify | multi-PV search that builds the solution | 1.5M nodes | `--verify-nodes/-time/-depth` |
| confirm | final re-check of every solver move | 5M nodes | `--confirm-nodes/-time/-depth`, `--no-confirm` |

Each engine runs single-threaded; `-j` sets how many run in parallel.

## How puzzles are found

1. **Scan** – every position of every game is evaluated quickly (`--scan-nodes`).
2. **Candidates** – a move is a candidate blunder when afterwards the opponent is at least
   `--min-eval` cp (default 200) ahead and his winning chance rose by at least `--min-jump` (default 0.3).
3. **Verification** – deep multi-PV search (`--verify-nodes`) for every solver move:
   * the best move must be **at least `--min-gap` cp (default 100) better than the second best move**,
     otherwise the puzzle is ambiguous;
   * two different mating moves are always ambiguous;
   * mate puzzles must be unique all the way to checkmate;
   * advantage puzzles continue while the solver's move stays unique and winning;
     the line always ends with a solver move;
   * one-move puzzles that are only a plain recapture are dropped (`--keep-recaptures`).
   The opponent's replies are the engine's best moves.
4. **Tactic filter** (not for mates)
   * the solution must win at least `--min-material` cp of material (default 200), so the point
     of the puzzle is visible in the solution;
   * the solution is cut after the solver move that wins the material – moves that only
     consolidate the advantage are dropped (`--no-trim` keeps them);
   * puzzles whose first move just takes a hanging piece (the capture's static exchange already
     wins all the material) are dropped (`--keep-free-captures` keeps them).
5. **Similar puzzles** – only the first of several similar puzzles is kept (`--keep-similar` keeps all).
   Two puzzles are similar when the same side starts with the same move and
   * ends with the same move (same motif, e.g. `Qa4+ … Qxb4`, whatever the opponent interposes), or
   * plays the same second move, or
   * the positions differ on at most `--similar-squares` squares (default 4).

   `--dedup` applies this to existing output files without running the engine.
6. **Confirmation** – every solver move is searched again with the longer confirm limit.
   The puzzle is dropped if the engine now prefers another move or the second best move
   comes within `--min-gap` cp.

## CSV columns

| Column | Meaning |
|---|---|
| PuzzleId | `<game no>-<ply of the blunder>` |
| GameNo | number of the game in the input PGN (1-based) |
| Event … GameId | game headers |
| FEN, Moves | lichess convention: position *before* the blunder, UCI moves starting with the blunder |
| PuzzleFEN | position after the blunder, solver to move |
| Solution | solution in SAN, e.g. `23. Qxh7+ Kxh7 24. Rh3#` |
| EvalBefore / EvalAfter / EvalFinal | evaluation from the solver's view before the blunder, after it, at the last solver move |
| MinGap | smallest cp gap between best and second best move along the solution (`mate` = only one legal move; values near 100000 = mate vs. non-mate) |
| Themes | `mate mateInN`, `advantage`/`crushing`, `oneMove`/`short`/`long`/`veryLong`, `opening`/`middlegame`/`endgame` |
