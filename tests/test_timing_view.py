"""Tests for app.timing_view: the pure helpers behind the Playground's timing panel.

The header comes off the network (and through whatever proxies sit in between), so most of this file is about input
that is odd or hostile: the parser must keep what is readable and never raise.
"""

import math

import pytest

from app.timing_view import (
    DETAIL_ORDER,
    MAX_HEADER_CHARS,
    MAX_METRICS,
    STEP_ORDER,
    build_breakdown,
    format_ms,
    format_share,
    label,
    order_steps,
    parse_server_timing,
    parse_traceresponse,
    short_label,
    slowest_step,
    split_timing,
    step_shares,
    trace_caption,
    trace_id_from_traceresponse,
    trace_url,
)


@pytest.fixture(autouse=True)
def _fake_redis():
    """Override the suite-wide fixture: it imports app.main (and loads the NER model); these tests need no Redis."""


ANONYMIZE_HEADER = (
    "app_config;dur=0.41, queue_wait;dur=0.12, nlp_pass1;dur=388.12, nlp_pass1.ner_model;dur=361.03, "
    "analyze;dur=15.80, analyze.context_enhance;dur=1.61, analyze.dedup;dur=0.70, recovery_pass2;dur=377.00, "
    "recovery_pass2.ner_model;dur=351.20, post_processing;dur=1.02, handling;dur=0.06, store_save;dur=2.41, "
    "other;dur=3.20, total;dur=788.30"
)
DEANONYMIZE_HEADER = "app_config;dur=0.40, store_get;dur=1.20, restore;dur=0.30, other;dur=0.50, total;dur=2.40"
TRACE_ID = "912d27582286f3a10ed6da832e654c39"
TRACEPARENT = f"00-{TRACE_ID}-00f067aa0ba902b7-01"


# ── parse_server_timing ──────────────────────────────────────────────────────


def test_parses_the_contract_header_in_order():
    timing = parse_server_timing(ANONYMIZE_HEADER)

    assert list(timing) == [
        "app_config", "queue_wait", "nlp_pass1", "nlp_pass1.ner_model", "analyze", "analyze.context_enhance",
        "analyze.dedup", "recovery_pass2", "recovery_pass2.ner_model", "post_processing", "handling", "store_save",
        "other", "total",
    ]  # fmt: skip
    assert timing["nlp_pass1"] == 388.12
    assert timing["total"] == 788.30


@pytest.mark.parametrize(
    "header, expected",
    [
        (" a ; dur = 1.5 ,\tb;dur=2 ,", {"a": 1.5, "b": 2.0}),  # optional whitespace everywhere the grammar allows it
        ("a;dur=1,\r\n b;dur=2", {"a": 1.0, "b": 2.0}),  # a folded or merged header line
        ("A;DUR=4, b;Dur=5", {"A": 4.0, "b": 5.0}),  # names keep their case; the parameter name does not matter
        ("a;dur=.5, b;dur=5., c;dur=1e2, d;dur=+3", {"a": 0.5, "b": 5.0, "c": 100.0, "d": 3.0}),
        ('a;dur="12.5"', {"a": 12.5}),  # a quoted number is legal
        ('cache;desc="Read, v2; hit \\"warm\\"";dur=23.2, db;dur=53', {"cache": 23.2, "db": 53.0}),
        ('x;dur=7;desc="a,b;c=d"', {"x": 7.0}),
        ("cdn-cache;desc=HIT, edge;dur=5, a;dur=1", {"edge": 5.0, "a": 1.0}),  # what a proxy may append is kept
    ],
)
def test_reads_what_the_grammar_allows(header, expected):
    assert parse_server_timing(header) == expected


def test_repeated_names_are_summed_in_place_of_the_first_occurrence():
    assert parse_server_timing("a;dur=1, b;dur=2, a;dur=2.5") == {"a": 3.5, "b": 2.0}


def test_the_first_dur_parameter_wins_and_a_bad_one_voids_the_metric():
    assert parse_server_timing("a;dur=1;dur=2") == {"a": 1.0}
    assert parse_server_timing("a;dur=x;dur=2, b;dur=3") == {"b": 3.0}


@pytest.mark.parametrize(
    "header",
    [
        "a, b;dur, c;dur=, d;dur=abc, e;desc=x, f;dur=\"",  # no dur, empty dur, not a number
        "a;dur=-1, b;dur=-0.001, c;dur=nan, d;dur=NaN, e;dur=inf, f;dur=-inf, g;dur=Infinity",
        "a;dur=1e999, b;dur=" + "9" * 10_000,  # overflow to infinity
        "a;dur=0x10, b;dur=1_0, c;dur=1 2, d;dur=١٢٣, e;dur=１２, f;dur=--1, g;dur=1e, h;dur=.",
    ],
)
def test_a_metric_without_a_usable_dur_is_skipped(header):
    assert parse_server_timing(header) == {}


def test_negative_zero_is_normalised():
    assert math.copysign(1.0, parse_server_timing("a;dur=-0")["a"]) == 1.0


def test_a_sum_that_overflows_keeps_the_last_finite_value():
    assert parse_server_timing("a;dur=1e308, a;dur=1e308") == {"a": 1e308}


@pytest.mark.parametrize("header", ["", " ", ",", ",,, ;;; ,", ";", "=", "=;=,=;=", "\r\n", "\x00", "\u200b"])
def test_empty_or_only_separators_gives_nothing(header):
    assert parse_server_timing(header) == {}


@pytest.mark.parametrize("value", [None, 123, 1.5, ["a;dur=1"], {"a": 1}, object()])
def test_a_value_that_is_not_text_gives_nothing(value):
    assert parse_server_timing(value) == {}


def test_bytes_are_read_as_latin_1():
    assert parse_server_timing(b"a;dur=1, caf\xe9;dur=2, b;dur=3") == {"a": 1.0, "b": 3.0}


def test_an_unterminated_quote_swallows_the_rest_but_keeps_what_came_before():
    assert parse_server_timing('a;dur=1;desc="oops, b;dur=2') == {"a": 1.0}
    assert parse_server_timing('a;desc="oops, b;dur=2') == {}


def test_a_stray_quote_cannot_swallow_the_metrics_after_it():
    assert parse_server_timing('a"b;dur=1, c;dur=2') == {"c": 2.0}
    assert parse_server_timing('a;dur=1;x"y, c;dur=2') == {"a": 1.0, "c": 2.0}


@pytest.mark.parametrize(
    "header",
    [
        "my step;dur=1",  # a space is not a token character
        "naïve;dur=1",
        "\u0430;dur=1",  # Cyrillic a
        "a/b;dur=1",
        "<script>;dur=1",
        "a\x00b;dur=1",
        ";dur=3",  # no name
        "x" * 65 + ";dur=1",  # absurdly long name
    ],
)
def test_a_metric_with_an_invalid_name_is_skipped_but_its_neighbours_are_not(header):
    assert parse_server_timing(f"{header}, ok;dur=2") == {"ok": 2.0}


def test_unicode_in_a_quoted_description_is_harmless():
    assert parse_server_timing('a;desc="héllo, wörld ✓";dur=3') == {"a": 3.0}


def test_huge_input_is_bounded_and_never_raises():
    cases = {
        "many metrics": "a;dur=1," * 2_000_000,
        "only quotes": '"' * 5_000_000,
        "only separators": ";," * 3_000_000,
        "never-closing description": "a;desc=" + '"' * 200_000,
        "a very long number": "a;dur=" + "7" * 5_000_000,
        "a very long name": "n" * 5_000_000 + ";dur=1",
    }
    for header in cases.values():
        assert len(parse_server_timing(header)) <= MAX_METRICS


def test_a_metric_cut_by_the_size_limit_is_dropped_not_misread():
    prefix = "a;dur=1," * ((MAX_HEADER_CHARS - len("z;dur=12")) // len("a;dur=1,"))
    assert len(prefix) + len("z;dur=12") == MAX_HEADER_CHARS  # the limit lands inside z's number
    timing = parse_server_timing(prefix + "z;dur=12345")
    assert "z" not in timing
    assert timing["a"] == (MAX_HEADER_CHARS - 8) // 8


def test_only_the_first_distinct_metrics_are_kept():
    header = ", ".join(f"m{i};dur=1" for i in range(MAX_METRICS + 20)) + ", m0;dur=4"
    timing = parse_server_timing(header)
    assert len(timing) == MAX_METRICS
    assert timing["m0"] == 5.0  # a name already kept can still be added to
    assert f"m{MAX_METRICS}" not in timing


# ── split_timing, ordering, shares, slowest ──────────────────────────────────


def test_split_timing_separates_steps_details_and_total():
    steps, details, total = split_timing(parse_server_timing(ANONYMIZE_HEADER))

    assert "total" not in steps and "nlp_pass1.ner_model" not in steps
    assert details == {
        "nlp_pass1.ner_model": 361.03,
        "analyze.context_enhance": 1.61,
        "analyze.dedup": 0.70,
        "recovery_pass2.ner_model": 351.20,
    }
    assert total == 788.30
    assert steps["analyze"] == 15.80


def test_split_timing_drops_values_that_cannot_be_durations():
    timing = {"a": float("nan"), "b": -1.0, "c": 2.0, "d.e": float("inf"), "total": "7", 5: 1.0, "f": None}
    assert split_timing(timing) == ({"c": 2.0}, {}, None)


def test_a_dot_at_either_end_does_not_make_a_detail():
    steps, details, _ = split_timing({".x": 1.0, "x.": 2.0, "x.y": 3.0})
    assert steps == {".x": 1.0, "x.": 2.0}
    assert details == {"x.y": 3.0}


def test_order_steps_follows_the_execution_order_of_the_contract():
    shuffled = ["other", "handling", "auth", "store_save", "nlp_pass1", "queue_wait", "analyze", "post_processing",
                "app_config", "recovery_pass2"]  # fmt: skip
    assert order_steps(shuffled) == [
        "auth", "app_config", "queue_wait", "nlp_pass1", "analyze", "recovery_pass2", "post_processing", "handling",
        "store_save", "other",
    ]  # fmt: skip
    assert order_steps(["restore", "other", "store_get", "app_config", "auth"]) == [
        "auth", "app_config", "store_get", "restore", "other",
    ]  # fmt: skip


def test_unknown_steps_come_last_in_the_order_they_were_given():
    assert order_steps(["zeta", "other", "alpha", "nlp_pass1"]) == ["nlp_pass1", "other", "zeta", "alpha"]


def test_shares_are_of_total_when_present():
    assert step_shares({"a": 25.0, "b": 75.0}, total=200.0) == {"a": 0.125, "b": 0.375}


@pytest.mark.parametrize("total", [None, 0.0, -5.0])
def test_shares_fall_back_to_the_sum_of_the_steps(total):
    assert step_shares({"a": 25.0, "b": 75.0}, total) == {"a": 0.25, "b": 0.75}


def test_shares_of_nothing_are_zero_not_a_division_error():
    assert step_shares({"a": 0.0, "b": 0.0}) == {"a": 0.0, "b": 0.0}
    assert step_shares({}) == {}
    assert step_shares({"a": 1.0}, total=float("inf")) == {"a": 0.0}


def test_slowest_step_prefers_the_earliest_on_a_tie():
    assert slowest_step({"a": 1.0, "b": 9.0, "c": 9.0}) == "b"
    assert slowest_step({"only": 0.0}) == "only"
    assert slowest_step({}) is None


# ── labels ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", [*STEP_ORDER, "total"])
def test_every_contract_step_has_a_friendly_label(name):
    assert label(name) != name
    assert short_label(name) != name
    assert len(short_label(name)) <= min(len(label(name)), 15)  # the metric tile truncates anything longer


@pytest.mark.parametrize("parent", ["nlp_pass1", "analyze", "recovery_pass2"])
@pytest.mark.parametrize("detail", DETAIL_ORDER)
def test_every_detail_key_has_a_friendly_label(parent, detail):
    assert label(f"{parent}.{detail}") not in (detail, f"{parent}.{detail}")


@pytest.mark.parametrize("name", ["nlp_pass9", "edge", "foo.bar", "analyze.mystery", "mystery.dedup", ""])
def test_an_unknown_name_is_shown_as_it_is(name):
    assert label(name) == name
    assert short_label(name) == name


# ── build_breakdown ──────────────────────────────────────────────────────────


def test_breakdown_of_the_contract_header():
    breakdown = build_breakdown(parse_server_timing(ANONYMIZE_HEADER))

    assert breakdown is not None
    assert (breakdown.total_ms, breakdown.total_reported) == (788.30, True)
    assert [row.name for row in breakdown.rows] == [
        "app_config", "queue_wait", "nlp_pass1", "nlp_pass1.ner_model", "analyze", "analyze.context_enhance",
        "analyze.dedup", "recovery_pass2", "recovery_pass2.ner_model", "post_processing", "handling", "store_save",
        "other",
    ]  # fmt: skip
    assert [row.name for row in breakdown.steps] == [
        "app_config", "queue_wait", "nlp_pass1", "analyze", "recovery_pass2", "post_processing", "handling",
        "store_save", "other",
    ]  # fmt: skip
    ner = next(row for row in breakdown.rows if row.name == "recovery_pass2.ner_model")
    assert (ner.parent, ner.is_detail, ner.label) == ("recovery_pass2", True, "NER model")
    assert breakdown.slowest is not None and breakdown.slowest.name == "nlp_pass1"
    assert breakdown.slowest.share == pytest.approx(388.12 / 788.30)
    assert ner.share == pytest.approx(351.20 / 788.30)  # a detail's share is of the total too, so it is not additive


def test_breakdown_without_the_second_pass_has_no_such_rows():
    header = "queue_wait;dur=0.1, nlp_pass1;dur=300, nlp_pass1.ner_model;dur=280, analyze;dur=12, handling;dur=1, total;dur=320"
    breakdown = build_breakdown(parse_server_timing(header))
    assert breakdown is not None
    assert not [row for row in breakdown.rows if row.name.startswith("recovery_pass2")]


def test_breakdown_of_a_deanonymize_header():
    breakdown = build_breakdown(parse_server_timing(DEANONYMIZE_HEADER))

    assert breakdown is not None
    assert [row.name for row in breakdown.rows] == ["app_config", "store_get", "restore", "other"]
    assert breakdown.slowest is not None and breakdown.slowest.name == "store_get"
    assert breakdown.total_ms == 2.40


def test_breakdown_without_total_uses_the_sum_of_the_steps():
    breakdown = build_breakdown({"nlp_pass1": 30.0, "nlp_pass1.ner_model": 20.0, "handling": 10.0})

    assert breakdown is not None
    assert (breakdown.total_ms, breakdown.total_reported) == (40.0, False)  # the detail is not added
    assert [row.share for row in breakdown.steps] == [0.75, 0.25]


def test_a_detail_whose_step_is_missing_is_still_shown_after_the_steps():
    breakdown = build_breakdown({"analyze": 5.0, "recovery_pass2.dedup": 1.0, "total": 10.0})

    assert breakdown is not None
    assert [(row.name, row.parent) for row in breakdown.rows] == [("analyze", None), ("recovery_pass2.dedup", "recovery_pass2")]
    assert [row.name for row in breakdown.steps] == ["analyze"]


def test_details_are_ordered_by_the_contract_not_by_their_position_in_the_header():
    breakdown = build_breakdown({"analyze.dedup": 1.0, "analyze.context_enhance": 2.0, "analyze": 5.0, "total": 8.0})
    assert breakdown is not None
    assert [row.name for row in breakdown.rows] == ["analyze", "analyze.context_enhance", "analyze.dedup"]


def test_unknown_steps_are_listed_after_the_known_ones_under_their_own_name():
    breakdown = build_breakdown(parse_server_timing("edge;dur=9, handling;dur=1, other;dur=0.5, total;dur=10.5"))
    assert breakdown is not None
    assert [(row.name, row.label) for row in breakdown.steps] == [
        ("handling", "Handling (replace / hash / fake)"), ("other", "Other"), ("edge", "edge"),
    ]  # fmt: skip
    assert breakdown.slowest is not None and breakdown.slowest.name == "edge"


def test_a_metric_longer_than_the_total_is_a_proxys_and_not_part_of_the_breakdown():
    timing = parse_server_timing("nlp_pass1;dur=300, other;dur=10, total;dur=310, cdn-origin;dur=420, edge;dur=5")

    breakdown = build_breakdown(timing)

    assert breakdown is not None
    assert [row.name for row in breakdown.steps] == ["nlp_pass1", "other", "edge"]  # cdn-origin includes the server
    assert breakdown.slowest is not None and breakdown.slowest.name == "nlp_pass1"


def test_a_step_the_api_defines_is_kept_even_if_it_exceeds_the_total():
    breakdown = build_breakdown({"nlp_pass1": 500.0, "total": 310.0})
    assert breakdown is not None and [row.name for row in breakdown.steps] == ["nlp_pass1"]


def test_the_slowest_step_is_never_a_detail():
    breakdown = build_breakdown({"nlp_pass1": 10.0, "nlp_pass1.ner_model": 9.0, "analyze": 1.0, "total": 11.0})
    assert breakdown is not None and breakdown.slowest is not None
    assert breakdown.slowest.name == "nlp_pass1"


@pytest.mark.parametrize(
    "timing",
    [
        {},
        {"analyze.dedup": 1.0},  # details alone say nothing
        {"a": float("nan"), "b": -3.0},
        {"total": float("nan")},
    ],
)
def test_nothing_usable_gives_no_breakdown(timing):
    assert build_breakdown(timing) is None


def test_a_total_alone_gives_a_breakdown_without_rows():
    breakdown = build_breakdown({"total": 12.5})
    assert breakdown is not None
    assert (breakdown.total_ms, breakdown.rows, breakdown.slowest) == (12.5, (), None)


def test_a_zero_total_falls_back_to_the_sum_for_the_shares():
    breakdown = build_breakdown({"a": 1.0, "b": 3.0, "total": 0.0})
    assert breakdown is not None
    assert [row.share for row in breakdown.rows] == [0.25, 0.75]


def test_build_breakdown_survives_arbitrary_parser_output():
    for header in ['"""', ";;;,,,", "a;dur=1e308, b;dur=1e308, c;dur=1e308", "total;dur=1e308, a;dur=1e308", "x" * 50_000]:
        breakdown = build_breakdown(parse_server_timing(header))
        if breakdown is not None:
            assert all(math.isfinite(row.share) for row in breakdown.rows)
            assert math.isfinite(breakdown.total_ms)


# ── formatting ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "ms, text",
    [
        (0.0, "0.00 ms"),
        (0.41, "0.41 ms"),
        (9.994, "9.99 ms"),
        (42.46, "42.5 ms"),
        (788.3, "788 ms"),
        (12_345.6, "12,346 ms"),
        (float("nan"), "n/a"),
        (float("inf"), "n/a"),
    ],
)
def test_format_ms(ms, text):
    assert format_ms(ms) == text


@pytest.mark.parametrize(
    "share, text",
    [(0.0, "0%"), (0.004, "<1%"), (0.0099, "<1%"), (0.01, "1%"), (0.492, "49%"), (1.0, "100%"), (float("nan"), "n/a")],
)
def test_format_share(share, text):
    assert format_share(share) == text


# ── traceresponse and trace links ────────────────────────────────────────────


def test_trace_id_is_read_from_a_traceresponse():
    assert trace_id_from_traceresponse(TRACEPARENT) == TRACE_ID
    parsed = parse_traceresponse(TRACEPARENT)
    assert parsed is not None
    assert (parsed.span_id, parsed.sampled) == ("00f067aa0ba902b7", True)


@pytest.mark.parametrize(
    "header",
    [
        f"  {TRACEPARENT.upper()}  ",  # case and whitespace
        f"garbage, {TRACEPARENT}, 00-{'1' * 32}-{'2' * 16}-01",  # merged values: the first valid one wins
        f"01-{TRACE_ID}-00f067aa0ba902b7-01-future-field",  # a later version may add fields
        TRACEPARENT.encode(),
    ],
)
def test_a_traceresponse_is_read_tolerantly(header):
    assert trace_id_from_traceresponse(header) == TRACE_ID


@pytest.mark.parametrize(
    "header",
    [
        None, "", "-", "---", "garbage", 7, [TRACEPARENT],
        f"ff-{TRACE_ID}-00f067aa0ba902b7-01",  # version ff is invalid
        f"00-{TRACE_ID}-00f067aa0ba902b7-01-extra",  # version 00 has exactly four fields
        f"00-{TRACE_ID}-00f067aa0ba902b7",
        f"00-{'0' * 32}-00f067aa0ba902b7-01",  # an all-zero trace id is invalid
        f"00-{TRACE_ID}-{'0' * 16}-01",
        f"00-{TRACE_ID[:-1]}-00f067aa0ba902b7-01",
        f"00-{TRACE_ID[:-1]}g-00f067aa0ba902b7-01",
        f"00-{TRACE_ID}-00f067aa0ba902b7-1",
        "00-" + "a" * 100_000,
    ],
)  # fmt: skip
def test_an_invalid_traceresponse_gives_no_trace_id(header):
    assert trace_id_from_traceresponse(header) is None


def test_the_sampled_flag_is_the_low_bit_of_the_trace_flags():
    assert parse_traceresponse(f"00-{TRACE_ID}-00f067aa0ba902b7-00").sampled is False
    assert parse_traceresponse(f"00-{TRACE_ID}-00f067aa0ba902b7-03").sampled is True


def test_trace_url_fills_the_template():
    assert trace_url("https://grafana.test/explore?q={trace_id}&org=1", TRACE_ID) == (
        f"https://grafana.test/explore?q={TRACE_ID}&org=1"
    )
    assert trace_url("  http://t/{trace_id}/{trace_id} \n", TRACE_ID) == f"http://t/{TRACE_ID}/{TRACE_ID}"


def test_trace_url_leaves_other_braces_alone():
    template = 'https://g.test/explore?left={"query":"{trace_id}"}'
    assert trace_url(template, TRACE_ID) == f'https://g.test/explore?left={{"query":"{TRACE_ID}"}}'


@pytest.mark.parametrize(
    "template",
    [
        "https://grafana.test/explore",  # no placeholder
        "https://grafana.test/{traceid}",
        "https://grafana.test/{TRACE_ID}",
        "",
        "   ",
        None,
        "javascript:alert('{trace_id}')",  # only http(s)
        "ftp://host/{trace_id}",
        "//host/{trace_id}",
        "/relative/{trace_id}",
        "https://host/a b/{trace_id}",  # whitespace and angle brackets must be percent-encoded
        "https://host/<{trace_id}>",
        "https://host/{trace_id}\nSet-Cookie: x=1",
    ],
)
def test_trace_url_rejects_unusable_templates(template):
    assert trace_url(template, TRACE_ID) is None


@pytest.mark.parametrize("trace_id", [None, "", "abc", TRACE_ID.upper(), "0" * 32, TRACE_ID + "0", 'x"><script>', TRACE_ID[:-1] + "/"])
def test_trace_url_only_ever_embeds_a_real_trace_id(trace_id):
    assert trace_url("https://grafana.test/{trace_id}", trace_id) is None


def test_trace_caption_without_a_trace_is_nothing():
    assert trace_caption(None, "https://grafana.test/{trace_id}") is None


def test_trace_caption_names_the_trace_and_links_it_when_a_template_is_set():
    trace = parse_traceresponse(TRACEPARENT)
    assert trace_caption(trace) == f"Trace ID `{TRACE_ID}`"
    assert trace_caption(trace, "  ") == f"Trace ID `{TRACE_ID}`"
    assert trace_caption(trace, "https://g.test/t/{trace_id}") == (
        f"Trace ID `{TRACE_ID}` · [Open trace](https://g.test/t/{TRACE_ID})"
    )


def test_trace_caption_keeps_parentheses_from_ending_the_markdown_link():
    caption = trace_caption(parse_traceresponse(TRACEPARENT), "https://g.test/q?f=(a)&t={trace_id}")
    assert caption is not None and "](https://g.test/q?f=%28a%29&t=" in caption


def test_trace_caption_offers_no_link_for_a_trace_that_was_not_sampled():
    trace = parse_traceresponse(f"00-{TRACE_ID}-00f067aa0ba902b7-00")
    caption = trace_caption(trace, "https://g.test/{trace_id}")
    assert caption is not None
    assert TRACE_ID in caption and "not sampled" in caption and "https://" not in caption


def test_trace_caption_says_when_the_template_is_unusable():
    caption = trace_caption(parse_traceresponse(TRACEPARENT), "not-a-url/{trace_id}")
    assert caption is not None and "TRACE_URL_TEMPLATE" in caption and "[Open trace]" not in caption
