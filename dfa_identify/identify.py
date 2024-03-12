from __future__ import annotations

from typing import Optional, Iterable

from dfa import DFA
from pysat.solvers import Glucose4

from pysat.card import CardEnc
from more_itertools import roundrobin

from dfa_identify.graphs import Word, APTA
from dfa_identify.encoding import dfa_id_encodings, Codec, SymMode
from dfa_identify.encoding import Bounds, ExtraClauseGenerator
from dfa_identify.encoding import (
    ColorAcceptingVar,
    ColorNodeVar,
    ParentRelationVar
)


def find_dfas(
        accepting: list[Word],
        rejecting: list[Word],
        solver_fact=Glucose4,
        sym_mode: SymMode = "bfs",
        extra_clauses: ExtraClauseGenerator = lambda *_: (),
        bounds: Bounds = (None, None),
        order_by_stutter: bool = False,
        alphabet: frozenset = None,
        allow_unminimized: bool = False,
) -> Iterable[DFA]:
    """Finds all minimal dfa that are consistent with the labeled examples.

    Here "minimal" means that a no DFA with smaller size is consistent with
    the data. Thus, all returns DFAs are the same size.

    Inputs:
      - accepting: A sequence of "words" to be accepted.
      - rejecting: A sequence of "words" to be rejected.
      - solver: A py-sat API compatible object for solving CNF SAT queries.
      - bounds: DFA size range (inclusive) to restrict search to, e.g.,
                - (None, 10): DFA can have as most 10 states.
                - (2, None): DFA must have at least 2 states.
                - (2, 10):  DFA must have between 2 and 10 states.
                - (None, None): No constraints (default).
      - sym_mode: Which symmetry breaking strategy to employ.
      - extra_clauses: Optional user defined additional clauses to add
          for a given codec (encoding of size k DFA).
      - order_by_stutter: Order DFA by number of self loop transitions.
      - alphabet: Optionally specify the alphabet the DFA should be over.
      - allow_unminimized: Continue after all minimized (equiv
          states merges) have been enumerated.

    Returns:
      An iterable of all minimal DFA consistent with accepting and rejecting.
    """
    models = find_models(accepting=accepting,
                         rejecting=rejecting,
                         solver_fact=solver_fact,
                         sym_mode=sym_mode,
                         extra_clauses=extra_clauses,
                         bounds=bounds,
                         order_by_stutter=order_by_stutter,
                         alphabet=alphabet,
                         allow_unminimized=allow_unminimized)
    yield from (codec.extract_dfa(m) for codec, m in models)


def find_models(
        accepting: list[Word],
        rejecting: list[Word],
        solver_fact=Glucose4,
        sym_mode: SymMode = "bfs",
        extra_clauses: ExtraClauseGenerator = lambda *_: (),
        bounds: Bounds = (None, None),
        order_by_stutter: bool = False,
        alphabet: frozenset = None,
        allow_unminimized: bool = False,
) -> Iterable[tuple[Codec, list[int]]]:
    # Convert to hashable words.
    accepting = list(map(tuple, accepting))
    rejecting = list(map(tuple, rejecting))

    if set(accepting) & set(rejecting):
        return
    elif len(accepting) == len(rejecting) == 0:
        if not alphabet:
            raise ValueError('Need examples or an alphabet!')

        # Conjecture empty string label and interleave dfas.
        kwargs = {
            'solver_fact': solver_fact, 'sym_mode': sym_mode,
            'extra_clauses': extra_clauses, 'bounds': bounds,
            'order_by_stutter': order_by_stutter, 'alphabet': alphabet,
            'allow_unminimized': allow_unminimized,
        }
        models_pos = find_models(accepting=[()], rejecting=[  ], **kwargs)
        models_neg = find_models(accepting=[  ], rejecting=[()], **kwargs)
        yield from roundrobin(models_pos, models_neg)
        return 

    apta = APTA.from_examples(
        accepting=accepting, rejecting=rejecting, alphabet=alphabet
    )
    encodings = dfa_id_encodings(
        apta=apta, sym_mode=sym_mode,
        extra_clauses=extra_clauses, bounds=bounds,
        allow_unminimized=allow_unminimized)

    for codec, clauses in encodings:
        with solver_fact(bootstrap_with=clauses) as solver:
            if not solver.solve():
                continue
            if not order_by_stutter:
                models = solver.enum_models()
                yield from ((codec, m) for m in models)
                if allow_unminimized:
                    continue
                return

            model = solver.get_model()  # Save for analysis below.

        # Search for maximally stuttering DFAs.
        models = order_models_by_stutter(solver_fact, codec, clauses, model)
        yield from ((codec, m) for m in models)
        if allow_unminimized:
            continue
        return


def find_dfa(
        accepting: list[Word],
        rejecting: list[Word],
        solver_fact=Glucose4,
        sym_mode: SymMode = "bfs",
        extra_clauses: ExtraClauseGenerator = lambda *_: (),
        bounds: Bounds = (None, None),
        order_by_stutter: bool = False,
        alphabet: frozenset = None,
) -> Optional[DFA]:
    """Finds a minimal dfa that is consistent with the labeled examples.

    Inputs:
      - accepting: A sequence of "words" to be accepted.
      - rejecting: A sequence of "words" to be rejected.
      - solver: A py-sat API compatible object for solving CNF SAT queries.
      - bounds: DFA size range (inclusive) to restrict search to, e.g.,
                - (None, 10): DFA can have as most 10 states.
                - (2, None): DFA must have at least 2 states.
                - (2, 10):  DFA must have between 2 and 10 states.
                - (None, None): No constraints (default).
      - sym_mode: Which symmetry breaking strategy to employ.
      - extra_clauses: Optional user defined additional clauses to add
          for a given codec (encoding of size k DFA).
      - order_by_stutter: Order DFA by number of self loop transitions.
      - alphabet: Optionally specify the alphabet the DFA should be over.

    Returns:
      Either a DFA consistent with accepting and rejecting or None
      indicating that no DFA exists.
    """
    all_dfas = find_dfas(
        accepting, rejecting, solver_fact, sym_mode, extra_clauses, bounds,
        order_by_stutter, alphabet
    )
    return next(all_dfas, None)


__all__ = ['DFA', 'find_dfas', 'find_dfa']


def order_models_by_stutter(
        solver_fact,
        codec: Codec,
        clauses: list[list[int]],
        model: list[int],
) -> Iterable[DFA]:
    # Compute the maximum id used in codec or by extra clauses.
    top_id = max(map(max, clauses))
    top_id = max(codec.max_id, top_id)

    # Compute parent relation variables that don't stutter.
    lits = codec.non_stutter_lits

    # Binary search for min non-stutter using cardinality constraints.

    def non_stutter_count(model) -> int:
        return sum(model[x - 1] > 0 for x in lits)

    def find_models(bound: int, make_formula):
        formula = make_formula(lits=lits, bound=bound, top_id=top_id)

        with solver_fact(bootstrap_with=clauses) as solver:
            solver.append_formula(formula, no_return=True)
            if not solver.solve():
                return
            yield from solver.enum_models()

    candidate_bound = non_stutter_count(model)  # Candidate upper bound.
    hi = candidate_bound     # Also upper bounds lower bound.
    lo = codec.n_colors - 1  # Each node needs to be visited.
    while lo < hi:
        mid = (lo + hi) // 2
        models = find_models(mid, CardEnc.atmost)
        witness = next(models, None)
        if witness is not None:
            hi = non_stutter_count(witness)
            assert hi <= mid
        else:
            lo = mid + 1

    # Incrementally emit models with less stutter.
    naive_bound = len(lits)
    for bound in range(lo, naive_bound + 1):
        if bound > candidate_bound:
            witness = next(find_models(bound, CardEnc.atmost), None)
            if witness is None:
                break
            candidate_bound = non_stutter_count(witness)

        yield from find_models(bound, CardEnc.equals)
