from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

from mypy.nodes import MatchStmt, NameExpr, TypeInfo
from mypy.patterns import (
    AsPattern,
    ClassPattern,
    MappingPattern,
    OrPattern,
    Pattern,
    SequencePattern,
    SingletonPattern,
    StarredPattern,
    ValuePattern,
)
from mypy.traverser import TraverserVisitor
from mypy.types import Instance, LiteralType, TupleType, get_proper_type
from mypyc.ir.ops import BasicBlock, Value
from mypyc.ir.rtypes import object_rprimitive
from mypyc.irbuild.builder import IRBuilder
from mypyc.primitives.dict_ops import (
    dict_copy,
    dict_del_item,
    mapping_has_key,
    supports_mapping_protocol,
)
from mypyc.primitives.generic_ops import generic_ssize_t_len_op
from mypyc.primitives.list_ops import (
    sequence_get_item,
    sequence_get_slice,
    supports_sequence_protocol,
)
from mypyc.primitives.misc_ops import fast_isinstance_op, slow_isinstance_op

# From: https://peps.python.org/pep-0634/#class-patterns
MATCHABLE_BUILTINS = {
    "builtins.bool",
    "builtins.bytearray",
    "builtins.bytes",
    "builtins.dict",
    "builtins.float",
    "builtins.frozenset",
    "builtins.int",
    "builtins.list",
    "builtins.set",
    "builtins.str",
    "builtins.tuple",
}


class MatchVisitor(TraverserVisitor):
    builder: IRBuilder
    code_block: BasicBlock
    next_block: BasicBlock
    final_block: BasicBlock
    subject: Value
    match: MatchStmt

    as_pattern: AsPattern | None = None

    def __init__(self, builder: IRBuilder, match_node: MatchStmt) -> None:
        self.builder = builder

        self.code_block = BasicBlock()
        self.next_block = BasicBlock()
        self.final_block = BasicBlock()

        self.match = match_node
        self.subject = builder.accept(match_node.subject)

    def build_match_body(self, index: int) -> None:
        self.builder.activate_block(self.code_block)

        guard = self.match.guards[index]

        if guard:
            self.code_block = BasicBlock()

            cond = self.builder.accept(guard)
            self.builder.add_bool_branch(cond, self.code_block, self.next_block)

            self.builder.activate_block(self.code_block)

        self.builder.accept(self.match.bodies[index])
        self.builder.goto(self.final_block)

    def visit_match_stmt(self, m: MatchStmt) -> None:
        for i, pattern in enumerate(m.patterns):
            self.code_block = BasicBlock()
            self.next_block = BasicBlock()

            pattern.accept(self)

            self.build_match_body(i)
            self.builder.activate_block(self.next_block)

        self.builder.goto_and_activate(self.final_block)

    def visit_value_pattern(self, pattern: ValuePattern) -> None:
        value = self.builder.accept(pattern.expr)

        cond = self.builder.binary_op(self.subject, value, "==", pattern.expr.line)

        self.bind_as_pattern(value)

        self.builder.add_bool_branch(cond, self.code_block, self.next_block)

    def visit_or_pattern(self, pattern: OrPattern) -> None:
        backup_block = self.next_block
        self.next_block = BasicBlock()

        for p in pattern.patterns:
            # Hack to ensure the as pattern is bound to each pattern in the
            # "or" pattern, but not every subpattern
            backup = self.as_pattern
            p.accept(self)
            self.as_pattern = backup

            self.builder.activate_block(self.next_block)
            self.next_block = BasicBlock()

        self.next_block = backup_block
        self.builder.goto(self.next_block)

    def visit_class_pattern(self, pattern: ClassPattern) -> None:
        # TODO: use faster instance check for native classes (while still
        # making sure to account for inheritance)
        isinstance_op = (
            fast_isinstance_op
            if self.builder.is_builtin_ref_expr(pattern.class_ref)
            else slow_isinstance_op
        )

        cond = self.builder.primitive_op(
            isinstance_op, [self.subject, self.builder.accept(pattern.class_ref)], pattern.line
        )

        self.builder.add_bool_branch(cond, self.code_block, self.next_block)

        self.bind_as_pattern(self.subject, new_block=True)

        if pattern.positionals:
            if pattern.class_ref.fullname in MATCHABLE_BUILTINS:
                self.builder.activate_block(self.code_block)
                self.code_block = BasicBlock()

                pattern.positionals[0].accept(self)

                return

            node = pattern.class_ref.node
            assert isinstance(node, TypeInfo), node
            match_args = extract_dunder_match_args_names(node)

            for i, expr in enumerate(pattern.positionals):
                self.builder.activate_block(self.code_block)
                self.code_block = BasicBlock()

                # TODO: use faster "get_attr" method instead when calling on native or
                # builtin objects
                positional = self.builder.py_get_attr(self.subject, match_args[i], expr.line)

                with self.enter_subpattern(positional):
                    expr.accept(self)

        for key, value in zip(pattern.keyword_keys, pattern.keyword_values):
            self.builder.activate_block(self.code_block)
            self.code_block = BasicBlock()

            # TODO: same as above "get_attr" comment
            attr = self.builder.py_get_attr(self.subject, key, value.line)

            with self.enter_subpattern(attr):
                value.accept(self)

    def visit_as_pattern(self, pattern: AsPattern) -> None:
        if pattern.pattern:
            old_pattern = self.as_pattern
            self.as_pattern = pattern
            pattern.pattern.accept(self)
            self.as_pattern = old_pattern

        elif pattern.name:
            target = self.builder.get_assignment_target(pattern.name)

            self.builder.assign(target, self.subject, pattern.line)

        self.builder.goto(self.code_block)

    def visit_singleton_pattern(self, pattern: SingletonPattern) -> None:
        if pattern.value is None:
            obj = self.builder.none_object()
        elif pattern.value is True:
            obj = self.builder.true()
        else:
            obj = self.builder.false()

        cond = self.builder.binary_op(self.subject, obj, "is", pattern.line)

        self.builder.add_bool_branch(cond, self.code_block, self.next_block)

    def visit_mapping_pattern(self, pattern: MappingPattern) -> None:
        is_dict = self.builder.call_c(supports_mapping_protocol, [self.subject], pattern.line)

        self.builder.add_bool_branch(is_dict, self.code_block, self.next_block)

        keys: list[Value] = []

        for key, value in zip(pattern.keys, pattern.values):
            self.builder.activate_block(self.code_block)
            self.code_block = BasicBlock()

            key_value = self.builder.accept(key)
            keys.append(key_value)

            exists = self.builder.call_c(mapping_has_key, [self.subject, key_value], pattern.line)

            self.builder.add_bool_branch(exists, self.code_block, self.next_block)
            self.builder.activate_block(self.code_block)
            self.code_block = BasicBlock()

            item = self.builder.gen_method_call(
                self.subject, "__getitem__", [key_value], object_rprimitive, pattern.line
            )

            with self.enter_subpattern(item):
                value.accept(self)

        if pattern.rest:
            self.builder.activate_block(self.code_block)
            self.code_block = BasicBlock()

            rest = self.builder.primitive_op(dict_copy, [self.subject], pattern.rest.line)

            target = self.builder.get_assignment_target(pattern.rest)

            self.builder.assign(target, rest, pattern.rest.line)

            for i, key_name in enumerate(keys):
                self.builder.call_c(dict_del_item, [rest, key_name], pattern.keys[i].line)

            self.builder.goto(self.code_block)

    def visit_sequence_pattern(self, seq_pattern: SequencePattern) -> None:
        star_index, capture, patterns = prep_sequence_pattern(seq_pattern)

        is_list = self.builder.call_c(supports_sequence_protocol, [self.subject], seq_pattern.line)

        self.builder.add_bool_branch(is_list, self.code_block, self.next_block)

        self.builder.activate_block(self.code_block)
        self.code_block = BasicBlock()

        actual_len = self.builder.call_c(generic_ssize_t_len_op, [self.subject], seq_pattern.line)
        min_len = len(patterns)

        is_long_enough = self.builder.binary_op(
            actual_len,
            self.builder.load_int(min_len),
            "==" if star_index is None else ">=",
            seq_pattern.line,
        )

        self.builder.add_bool_branch(is_long_enough, self.code_block, self.next_block)

        for i, pattern in enumerate(patterns):
            self.builder.activate_block(self.code_block)
            self.code_block = BasicBlock()

            if star_index is not None and i >= star_index:
                current = self.builder.binary_op(
                    actual_len, self.builder.load_int(min_len - i), "-", pattern.line
                )

            else:
                current = self.builder.load_int(i)

            item = self.builder.call_c(sequence_get_item, [self.subject, current], pattern.line)

            with self.enter_subpattern(item):
                pattern.accept(self)

        if capture and star_index is not None:
            self.builder.activate_block(self.code_block)
            self.code_block = BasicBlock()

            capture_end = self.builder.binary_op(
                actual_len, self.builder.load_int(min_len - star_index), "-", capture.line
            )

            rest = self.builder.call_c(
                sequence_get_slice,
                [self.subject, self.builder.load_int(star_index), capture_end],
                capture.line,
            )

            target = self.builder.get_assignment_target(capture)
            self.builder.assign(target, rest, capture.line)

            self.builder.goto(self.code_block)

    def bind_as_pattern(self, value: Value, new_block: bool = False) -> None:
        if self.as_pattern and self.as_pattern.pattern and self.as_pattern.name:
            if new_block:
                self.builder.activate_block(self.code_block)
                self.code_block = BasicBlock()

            target = self.builder.get_assignment_target(self.as_pattern.name)
            self.builder.assign(target, value, self.as_pattern.pattern.line)

            self.as_pattern = None

            if new_block:
                self.builder.goto(self.code_block)

    @contextmanager
    def enter_subpattern(self, subject: Value) -> Generator[None]:
        old_subject = self.subject
        self.subject = subject
        yield
        self.subject = old_subject


def prep_sequence_pattern(
    seq_pattern: SequencePattern,
) -> tuple[int | None, NameExpr | None, list[Pattern]]:
    star_index: int | None = None
    capture: NameExpr | None = None
    patterns: list[Pattern] = []

    for i, pattern in enumerate(seq_pattern.patterns):
        if isinstance(pattern, StarredPattern):
            star_index = i
            capture = pattern.capture

        else:
            patterns.append(pattern)

    return star_index, capture, patterns


def extract_dunder_match_args_names(info: TypeInfo) -> list[str]:
    ty = info.names.get("__match_args__")
    assert ty
    match_args_type = get_proper_type(ty.type)
    assert isinstance(match_args_type, TupleType), match_args_type

    match_args: list[str] = []
    for item in match_args_type.items:
        proper_item = get_proper_type(item)

        match_arg = None
        if isinstance(proper_item, Instance) and proper_item.last_known_value:
            match_arg = proper_item.last_known_value.value
        elif isinstance(proper_item, LiteralType):
            match_arg = proper_item.value
        assert isinstance(match_arg, str), f"Unrecognized __match_args__ item: {item}"

        match_args.append(match_arg)
    return match_args
