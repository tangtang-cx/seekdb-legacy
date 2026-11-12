"""Tokenizer and recursive-descent parser for the SQL subset the task requires.

Supported:
    CREATE TABLE t (c BIGINT PRIMARY KEY, s VARCHAR(16), v VECTOR(2))
    CREATE VECTOR INDEX idx ON t(v)
    INSERT INTO t VALUES (...), (...)
    UPDATE t SET c = expr, ... [WHERE cond]
    DELETE FROM t [WHERE cond]
    SELECT * | col | expr [AS alias], ... FROM t [WHERE cond] [ORDER BY ...] [LIMIT n]
    BEGIN / COMMIT / ROLLBACK

Everything else raises SqlError(unsupported_sql) or SqlError(syntax_error).
"""
from __future__ import annotations

import re
from typing import Any

from .core import (BIGINT, VECTOR, VARCHAR, Column, SqlError, SYNTAX_ERROR,
                   UNSUPPORTED_SQL, parse_vector_literal)

TOKEN_RE = re.compile(r"""
    (?P<ws>\s+)
  | (?P<line_comment>--[^\n]*|\#[^\n]*)
  | (?P<block_comment>/\*.*?\*/)
  | (?P<number>\d+\.\d+(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?|\d+(?:[eE][+-]?\d+)?)
  | (?P<string>'(?:''|\\.|[^'\\])*')
  | (?P<ident>`[^`]*`|[A-Za-z_][A-Za-z0-9_$]*)
  | (?P<punct><>|!=|<=|>=|:=|\(|\)|,|;|=|<|>|\*|\+|-|\[|\]|\.)
""", re.VERBOSE | re.DOTALL)

# A vector literal is scanned as one token instead of one token per number.
# A query with a 128 dimension vector would otherwise cost hundreds of
# regular expression matches and token objects before the executor sees it.
VECTOR_RE = re.compile(
    r"\[\s*[-+]?(?:\d+\.\d+(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?|\d+(?:[eE][+-]?\d+)?)"
    r"(?:\s*,\s*[-+]?(?:\d+\.\d+(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?|\d+(?:[eE][+-]?\d+)?))*"
    r"\s*\]"
)

KEYWORDS = {
    "SELECT", "FROM", "WHERE", "ORDER", "BY", "LIMIT", "INSERT", "INTO",
    "VALUES", "UPDATE", "SET", "DELETE", "CREATE", "TABLE", "INDEX", "ON",
    "BEGIN", "COMMIT", "ROLLBACK", "AND", "OR", "NOT", "NULL", "IS", "AS",
    "ASC", "DESC", "PRIMARY", "KEY", "BIGINT", "VARCHAR", "VECTOR", "IN",
    "START", "TRANSACTION", "WORK", "REPLACE", "INTO", "DROP", "OFFSET",
}

#: Aggregate functions are one of the statement kinds the task lists as
#: unsupported.  They are recognised by name so that the statement fails with
#: the agreed error instead of a generic syntax error.
AGGREGATE_FUNCTIONS = {"COUNT", "SUM", "AVG", "MIN", "MAX", "GROUP_CONCAT"}

#: Words that introduce a join.  A join is not part of the supported subset.
JOIN_WORDS = {"JOIN", "INNER", "LEFT", "RIGHT", "FULL", "CROSS", "NATURAL",
              "STRAIGHT_JOIN"}


class Token:
    __slots__ = ("kind", "value", "pos")

    def __init__(self, kind: str, value: Any, pos: int) -> None:
        self.kind = kind
        self.value = value
        self.pos = pos

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"Token({self.kind!r}, {self.value!r})"


def tokenize(sql: str) -> list[Token]:
    tokens: list[Token] = []
    pos = 0
    length = len(sql)
    while pos < length:
        if sql[pos] == "[":
            vector = VECTOR_RE.match(sql, pos)
            if vector:
                tokens.append(Token("vector", _vector_values(vector.group()), pos))
                pos = vector.end()
                continue
        if sql[pos] == "'":
            # A quoted literal is scanned without the regular expression: a
            # bulk insert carries one literal of a few hundred digits per
            # row, and matching that with the token pattern costs more than
            # the rest of the statement.
            end = _scan_string(sql, pos)
            tokens.append(Token("string", _decode_string(sql[pos:end]), pos))
            pos = end
            continue
        match = TOKEN_RE.match(sql, pos)
        if not match:
            raise SqlError(SYNTAX_ERROR, f"unexpected character at {pos}: {sql[pos]!r}")
        kind = match.lastgroup
        text = match.group()
        pos = match.end()
        if kind in ("ws", "line_comment", "block_comment"):
            continue
        if kind == "number":
            if "." in text or "e" in text or "E" in text:
                tokens.append(Token("number", float(text), match.start()))
            else:
                tokens.append(Token("number", int(text), match.start()))
        elif kind == "ident":
            bare = text[1:-1] if text.startswith("`") else text
            upper = bare.upper()
            tokens.append(Token("keyword" if upper in KEYWORDS else "ident", bare, match.start()))
        else:
            tokens.append(Token("punct", text, match.start()))
    tokens.append(Token("eof", None, length))
    return tokens


def _scan_string(sql: str, start: int) -> int:
    """The offset just past the string literal that starts at ``start``.

    A quote ends the literal unless it is doubled or preceded by an odd
    number of backslashes, which is exactly what the token pattern accepts.
    """

    length = len(sql)
    pos = start + 1
    while True:
        pos = sql.find("'", pos)
        if pos < 0:
            raise SqlError(SYNTAX_ERROR, "unterminated string literal")
        backslashes = 0
        index = pos - 1
        while index > start and sql[index] == "\\":
            backslashes += 1
            index -= 1
        if backslashes % 2:
            pos += 1
            continue
        if sql[pos + 1:pos + 2] == "'":
            pos += 2
            continue
        return pos + 1


def _vector_values(text: str) -> list[float]:
    """The numbers of a vector literal, in the order they appear."""

    body = text[1:-1]
    values: list[float] = []
    for token in body.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            values.append(float(token))
        except ValueError:
            raise SqlError(SYNTAX_ERROR, "vector literal expects numbers") from None
    return values


def _decode_string(raw: str) -> str:
    body = raw[1:-1]
    # The common case is a literal without escapes.  Unescaping it character
    # by character is the most expensive part of tokenising a bulk INSERT,
    # because every vector literal is a string of several hundred digits.
    if "\\" not in body and "'" not in body:
        return body
    out: list[str] = []
    index = 0
    while index < len(body):
        char = body[index]
        if char == "\\" and index + 1 < len(body):
            nxt = body[index + 1]
            mapping = {"n": "\n", "t": "\t", "r": "\r", "0": "\0", "\\": "\\",
                       "'": "'", '"': '"', "b": "\b", "Z": "\x1a"}
            out.append(mapping.get(nxt, nxt))
            index += 2
            continue
        if char == "'" and index + 1 < len(body) and body[index + 1] == "'":
            out.append("'")
            index += 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


# ---------------------------------------------------------------- AST nodes
#
# Expressions are plain tuples so evaluation stays allocation-light:
#   ("col", name) ("lit", value) ("vec", [floats]) ("func", name, [args])
#   ("binop", op, left, right) ("and", l, r) ("or", l, r) ("not", e) ("isnull", e)

class Parser:
    def __init__(self, sql: str) -> None:
        self.sql = sql
        self.tokens = tokenize(sql)
        self.index = 0

    # -- helpers
    def peek(self, offset: int = 0) -> Token:
        return self.tokens[min(self.index + offset, len(self.tokens) - 1)]

    def next(self) -> Token:
        token = self.tokens[self.index]
        if token.kind != "eof":
            self.index += 1
        return token

    def at_keyword(self, *words: str) -> bool:
        token = self.peek()
        return token.kind == "keyword" and token.value.upper() in words

    def eat_keyword(self, *words: str) -> bool:
        if self.at_keyword(*words):
            self.next()
            return True
        return False

    def expect_keyword(self, *words: str) -> None:
        if not self.eat_keyword(*words):
            raise SqlError(SYNTAX_ERROR, f"expected {'/'.join(words)} near {self.peek().value!r}")

    def at_punct(self, text: str) -> bool:
        token = self.peek()
        return token.kind == "punct" and token.value == text

    def eat_punct(self, text: str) -> bool:
        if self.at_punct(text):
            self.next()
            return True
        return False

    def expect_punct(self, text: str) -> None:
        if not self.eat_punct(text):
            raise SqlError(SYNTAX_ERROR, f"expected {text!r} near {self.peek().value!r}")

    def identifier(self) -> str:
        token = self.peek()
        if token.kind in ("ident", "keyword"):
            self.next()
            return str(token.value)
        raise SqlError(SYNTAX_ERROR, f"expected identifier near {token.value!r}")

    # -- statements
    def parse_statement(self) -> dict:
        self.eat_punct(";")
        token = self.peek()
        if token.kind != "keyword":
            raise SqlError(UNSUPPORTED_SQL, f"unsupported statement: {self.sql.strip()[:60]!r}")
        word = str(token.value).upper()
        if word == "CREATE":
            return self.parse_create()
        if word == "INSERT":
            return self.parse_insert()
        if word == "UPDATE":
            return self.parse_update()
        if word == "DELETE":
            return self.parse_delete()
        if word == "DROP":
            return self.parse_drop()
        if word == "SELECT":
            return self.parse_select()
        if word in ("BEGIN", "START"):
            self.next()
            self.eat_keyword("TRANSACTION")
            self.eat_keyword("WORK")
            self.eat_punct(";")
            return {"kind": "begin"}
        if word == "COMMIT":
            self.next()
            self.eat_keyword("TRANSACTION")
            self.eat_keyword("WORK")
            self.eat_punct(";")
            return {"kind": "commit"}
        if word == "ROLLBACK":
            self.next()
            self.eat_keyword("TRANSACTION")
            self.eat_keyword("WORK")
            self.eat_punct(";")
            return {"kind": "rollback"}
        raise SqlError(UNSUPPORTED_SQL, f"unsupported statement: {word}")

    def parse_create(self) -> dict:
        self.expect_keyword("CREATE")
        if self.at_keyword("TABLE"):
            return self.parse_create_table()
        if self.at_keyword("VECTOR", "UNIQUE", "INDEX", "FULLTEXT"):
            self.eat_keyword("VECTOR", "UNIQUE", "FULLTEXT")
            self.expect_keyword("INDEX")
            name = self.identifier()
            self.expect_keyword("ON")
            table = self.identifier()
            self.expect_punct("(")
            column = self.identifier()
            self.expect_punct(")")
            self.eat_punct(";")
            return {"kind": "create_index", "name": name, "table": table, "column": column}
        raise SqlError(UNSUPPORTED_SQL, f"unsupported CREATE near {self.peek().value!r}")

    def parse_create_table(self) -> dict:
        self.expect_keyword("TABLE")
        table = self.identifier()
        self.expect_punct("(")
        columns: list[Column] = []
        while True:
            name = self.identifier()
            token = self.peek()
            if token.kind != "keyword":
                raise SqlError(SYNTAX_ERROR, f"expected column type near {token.value!r}")
            type_name = str(token.value).upper()
            self.next()
            length = dim = None
            if self.at_punct("("):
                self.next()
                number = self.next()
                if number.kind != "number" or not isinstance(number.value, int):
                    raise SqlError(SYNTAX_ERROR, "column length/dimension must be an integer")
                if type_name == VARCHAR:
                    length = number.value
                elif type_name == VECTOR:
                    dim = number.value
                    if dim <= 0:
                        raise SqlError(SYNTAX_ERROR, "vector dimension must be positive")
                else:
                    raise SqlError(UNSUPPORTED_SQL, f"{type_name} does not take a length")
                self.expect_punct(")")
            # A foreign key is one of the statement kinds the task lists as
            # unsupported.
            token = self.peek()
            if token.kind in ("keyword", "ident") and str(token.value).upper() == "REFERENCES":
                raise SqlError(UNSUPPORTED_SQL, "foreign keys are not supported")
            primary = False
            if self.at_keyword("PRIMARY"):
                self.next()
                self.expect_keyword("KEY")
                primary = True
            if self.at_keyword("NOT"):
                self.next()
                self.expect_keyword("NULL")
            columns.append(Column(name, type_name, length, dim, primary))
            if not self.eat_punct(","):
                break
        self.expect_punct(")")
        self.eat_punct(";")
        if not columns:
            raise SqlError(SYNTAX_ERROR, "table needs at least one column")
        return {"kind": "create_table", "table": table, "columns": columns}

    def parse_insert(self) -> dict:
        self.expect_keyword("INSERT")
        self.expect_keyword("INTO")
        table = self.identifier()
        columns: list[str] | None = None
        if self.at_punct("("):
            self.next()
            columns = []
            while True:
                columns.append(self.identifier())
                if not self.eat_punct(","):
                    break
            self.expect_punct(")")
        if self.at_keyword("SELECT"):
            # INSERT INTO t [(cols)] SELECT ... FROM other
            source = self.parse_select()
            return {"kind": "insert_select", "table": table, "columns": columns,
                    "select": source}
        self.expect_keyword("VALUES")
        rows: list[list[Any]] = []
        while True:
            self.expect_punct("(")
            values: list[Any] = []
            while True:
                values.append(self.parse_literal())
                if not self.eat_punct(","):
                    break
            self.expect_punct(")")
            rows.append(values)
            if not self.eat_punct(","):
                break
        self.eat_punct(";")
        return {"kind": "insert", "table": table, "columns": columns, "rows": rows}

    def parse_update(self) -> dict:
        self.expect_keyword("UPDATE")
        table = self.identifier()
        self.expect_keyword("SET")
        assignments: list[tuple[str, Any]] = []
        while True:
            column = self.identifier()
            self.expect_punct("=")
            assignments.append((column, self.parse_expression()))
            if not self.eat_punct(","):
                break
        where = None
        if self.eat_keyword("WHERE"):
            where = self.parse_expression()
        self.eat_punct(";")
        return {"kind": "update", "table": table, "assignments": assignments, "where": where}

    def parse_delete(self) -> dict:
        self.expect_keyword("DELETE")
        self.expect_keyword("FROM")
        table = self.identifier()
        where = None
        if self.eat_keyword("WHERE"):
            where = self.parse_expression()
        self.eat_punct(";")
        return {"kind": "delete", "table": table, "where": where}

    def parse_drop(self) -> dict:
        self.expect_keyword("DROP")
        if self.at_keyword("TABLE"):
            self.next()
            if self.at_keyword("IF"):
                self.next()
                self.expect_keyword("EXISTS")
            table = self.identifier()
            self.eat_punct(";")
            return {"kind": "drop_table", "table": table}
        if self.at_keyword("VECTOR", "INDEX"):
            self.eat_keyword("VECTOR", "INDEX")
            name = self.identifier()
            table = None
            if self.eat_keyword("ON"):
                table = self.identifier()
            self.eat_punct(";")
            return {"kind": "drop_index", "name": name, "table": table}
        raise SqlError(UNSUPPORTED_SQL, f"unsupported DROP near {self.peek().value!r}")

    def parse_select(self) -> dict:
        self.expect_keyword("SELECT")
        items: list[dict] = []
        if self.eat_punct("*"):
            items.append({"expr": ("star",), "alias": None})
        else:
            while True:
                expr = self.parse_expression()
                alias = None
                if self.eat_keyword("AS"):
                    alias = self.identifier()
                items.append({"expr": expr, "alias": alias})
                if not self.eat_punct(","):
                    break
        self.expect_keyword("FROM")
        table = self.identifier()
        self._reject_join()
        where = None
        if self.eat_keyword("WHERE"):
            where = self.parse_expression()
        order: list[tuple[Any, bool]] = []
        if self.eat_keyword("ORDER"):
            self.expect_keyword("BY")
            while True:
                expr = self.parse_expression()
                descending = False
                if self.eat_keyword("DESC"):
                    descending = True
                else:
                    self.eat_keyword("ASC")
                order.append((expr, descending))
                if not self.eat_punct(","):
                    break
        limit = None
        offset = 0
        if self.eat_keyword("LIMIT"):
            token = self.next()
            if token.kind != "number" or not isinstance(token.value, int):
                raise SqlError(SYNTAX_ERROR, "LIMIT expects an integer")
            limit = token.value
            if self.eat_keyword("OFFSET"):
                token = self.next()
                if token.kind != "number" or not isinstance(token.value, int):
                    raise SqlError(SYNTAX_ERROR, "OFFSET expects an integer")
                offset = token.value
        self.eat_punct(";")
        return {"kind": "select", "items": items, "table": table, "where": where,
                "order": order, "limit": limit, "offset": offset}

    def _reject_join(self) -> None:
        """A join is not part of the supported subset.

        The check looks at the token text, so it also catches the qualifiers
        (``INNER``, ``LEFT``, ...) that precede the join keyword.
        """

        token = self.peek()
        if token.kind in ("keyword", "ident") and str(token.value).upper() in JOIN_WORDS:
            raise SqlError(UNSUPPORTED_SQL, "joins are not supported")

    # -- expressions
    def parse_literal(self) -> Any:
        token = self.peek()
        if self.at_punct("-"):
            self.next()
            inner = self.parse_literal()
            if inner[0] == "lit" and isinstance(inner[1], (int, float)):
                return ("lit", -inner[1])
            raise SqlError(SYNTAX_ERROR, "unsupported unary minus")
        if token.kind == "number":
            self.next()
            return ("lit", token.value)
        if token.kind == "string":
            self.next()
            return ("str", token.value)
        if token.kind == "vector":
            self.next()
            return ("vec", token.value)
        if self.at_punct("["):
            self.next()
            values: list[float] = []
            if not self.at_punct("]"):
                while True:
                    sign = 1.0
                    if self.at_punct("-"):
                        self.next()
                        sign = -1.0
                    elif self.at_punct("+"):
                        self.next()
                    item = self.next()
                    if item.kind != "number":
                        raise SqlError(SYNTAX_ERROR, "vector literal expects numbers")
                    values.append(sign * float(item.value))
                    if not self.eat_punct(","):
                        break
            self.expect_punct("]")
            return ("vec", values)
        if self.at_keyword("NULL"):
            self.next()
            return ("lit", None)
        raise SqlError(SYNTAX_ERROR, f"unexpected value near {token.value!r}")

    def parse_expression(self) -> Any:
        return self.parse_or()

    def parse_or(self) -> Any:
        left = self.parse_and()
        while self.eat_keyword("OR"):
            left = ("or", left, self.parse_and())
        return left

    def parse_and(self) -> Any:
        left = self.parse_not()
        while self.eat_keyword("AND"):
            left = ("and", left, self.parse_not())
        return left

    def parse_not(self) -> Any:
        if self.eat_keyword("NOT"):
            return ("not", self.parse_not())
        return self.parse_comparison()

    def parse_comparison(self) -> Any:
        left = self.parse_additive()
        token = self.peek()
        if token.kind == "punct" and token.value in ("=", "<>", "!=", "<", "<=", ">", ">="):
            self.next()
            op = "!=" if token.value == "!=" else token.value
            right = self.parse_additive()
            return ("binop", op, left, right)
        if self.at_keyword("IS"):
            self.next()
            negate = self.eat_keyword("NOT")
            self.expect_keyword("NULL")
            expr = ("isnull", left)
            return ("not", expr) if negate else expr
        if self.at_keyword("IN"):
            self.next()
            self.expect_punct("(")
            items = []
            while True:
                items.append(self.parse_primary())
                if not self.eat_punct(","):
                    break
            self.expect_punct(")")
            return ("in", left, items)
        return left

    def parse_additive(self) -> Any:
        """Addition and subtraction, e.g. ``SET value = value + 1``."""

        left = self.parse_primary()
        while True:
            token = self.peek()
            if token.kind != "punct" or token.value not in ("+", "-"):
                return left
            self.next()
            right = self.parse_primary()
            left = ("arith", token.value, left, right)

    def parse_primary(self) -> Any:
        token = self.peek()
        if token.kind == "punct" and token.value == "(":
            self.next()
            expr = self.parse_expression()
            self.expect_punct(")")
            return expr
        if token.kind == "punct" and token.value == "-":
            self.next()
            inner = self.parse_primary()
            if inner[0] == "lit" and isinstance(inner[1], (int, float)):
                return ("lit", -inner[1])
            return ("neg", inner)
        if token.kind == "number":
            self.next()
            return ("lit", token.value)
        if token.kind == "string":
            self.next()
            return ("str", token.value)
        if token.kind == "vector":
            self.next()
            return ("vec", token.value)
        if token.kind == "punct" and token.value == "[":
            return self.parse_literal()
        if token.kind == "ident":
            name = str(token.value)
            self.next()
            if self.at_punct("("):
                if name.upper() in AGGREGATE_FUNCTIONS:
                    # An aggregate is one of the statement kinds the task lists
                    # as unsupported.
                    raise SqlError(UNSUPPORTED_SQL,
                                   f"aggregate function {name.upper()} is not supported")
                self.next()
                args: list[Any] = []
                if not self.at_punct(")"):
                    while True:
                        args.append(self.parse_expression())
                        if not self.eat_punct(","):
                            break
                self.expect_punct(")")
                return ("func", name.upper(), args)
            return ("col", name)
        if token.kind == "keyword":
            word = str(token.value).upper()
            if word == "NULL":
                self.next()
                return ("lit", None)
            if word in ("TRUE", "FALSE"):
                self.next()
                return ("lit", word == "TRUE")
            if word in ("SELECT", "INSERT", "UPDATE", "DELETE", "CREATE", "ALTER",
                        "DROP", "SET", "SHOW", "USE", "GRANT", "REVOKE", "TRUNCATE"):
                raise SqlError(UNSUPPORTED_SQL, f"unsupported expression near {word}")
        raise SqlError(SYNTAX_ERROR, f"unexpected token near {token.value!r}")


def parse(sql: str) -> dict:
    parser = Parser(sql)
    statement = parser.parse_statement()
    if parser.peek().kind != "eof":
        raise SqlError(SYNTAX_ERROR, f"trailing input near {parser.peek().value!r}")
    return statement


__all__ = ["parse", "tokenize", "Parser", "Token", "parse_vector_literal"]
