"""Reading the active picture out of a Dolby Vision RPU.

The three payloads below were copied out of dovi_tool rather than written by
hand, for the reason ``test_probe.py`` gives about its ffprobe fixtures: a
document invented from the documentation would agree with whatever this module
happened to do. They come from two releases of the same 2026 film -- a UHD disc
remux and an iTunes WEB-DL -- and from an RPU deliberately edited with
``dovi_tool editor`` to change shape part way through, which is how a film with
an IMAX sequence behaves and is the case a single reading cannot see.

Note what the real blocks look like: level 5 arrives among six or seven
siblings, three of them repeats of Level2, and the WEB-DL's list has no Level4
either. A parser that assumed a fixed position or a fixed set would pass on one
of these and fail on the next.
"""

from __future__ import annotations

import pytest

from kiyas.media import rpu

# --------------------------------------------------------------------------
# Captured payloads
# --------------------------------------------------------------------------

#: UHD disc remux, profile 7. A 2.39:1 film in a 16:9 frame: 276 rows masked
#: top and bottom.
REMUX_SCOPE = {
    "vdr_dm_data": {
        "cmv29_metadata": {
            "ext_metadata_blocks": [
                {"Level1": {"min_pq": 0, "max_pq": 3079, "avg_pq": 2048}},
                {
                    "Level2": {
                        "target_max_pq": 2081,
                        "trim_slope": 1397,
                        "trim_offset": 2019,
                        "trim_power": 1197,
                        "trim_chroma_weight": 2048,
                        "trim_saturation_gain": 2048,
                        "ms_weight": 2048,
                    }
                },
                {"Level4": {"anchor_pq": 2, "anchor_power": 1}},
                {
                    "Level5": {
                        "active_area_left_offset": 0,
                        "active_area_right_offset": 0,
                        "active_area_top_offset": 276,
                        "active_area_bottom_offset": 276,
                    }
                },
                {
                    "Level6": {
                        "max_display_mastering_luminance": 1000,
                        "min_display_mastering_luminance": 1,
                        "max_content_light_level": 266,
                        "max_frame_average_light_level": 109,
                    }
                },
            ]
        }
    }
}

#: The same RPU after ``dovi_tool editor`` opened the frame out, which is what
#: an IMAX sequence looks like in the metadata.
REMUX_OPENED_OUT = {
    "vdr_dm_data": {
        "cmv29_metadata": {
            "ext_metadata_blocks": [
                {"Level1": {"min_pq": 0, "max_pq": 2141, "avg_pq": 1229}},
                {"Level4": {"anchor_pq": 930, "anchor_power": 458}},
                {
                    "Level5": {
                        "active_area_left_offset": 0,
                        "active_area_right_offset": 0,
                        "active_area_top_offset": 0,
                        "active_area_bottom_offset": 0,
                    }
                },
            ]
        }
    }
}

#: iTunes WEB-DL, profile 8.1, already cropped to its own picture. No level 5
#: at all -- there is nothing left to mask, so the block has no reason to exist.
WEB_DL_NO_LEVEL5 = {
    "vdr_dm_data": {
        "cmv29_metadata": {
            "ext_metadata_blocks": [
                {"Level1": {"min_pq": 12, "max_pq": 2081, "avg_pq": 1229}},
                {
                    "Level2": {
                        "target_max_pq": 2081,
                        "trim_slope": 1906,
                        "trim_offset": 2032,
                        "trim_power": 1407,
                        "trim_chroma_weight": 2048,
                        "trim_saturation_gain": 2048,
                        "ms_weight": 2048,
                    }
                },
                {
                    "Level6": {
                        "max_display_mastering_luminance": 1000,
                        "min_display_mastering_luminance": 1,
                        "max_content_light_level": 0,
                        "max_frame_average_light_level": 0,
                    }
                },
            ]
        }
    }
}


# --------------------------------------------------------------------------
# Reading one frame
# --------------------------------------------------------------------------


def test_the_active_area_is_found_among_its_siblings():
    assert rpu.read_active_area(REMUX_SCOPE) == rpu.ActiveArea(0, 0, 276, 276)


def test_a_frame_with_no_level_5_reads_as_no_masking():
    assert rpu.read_active_area(WEB_DL_NO_LEVEL5) is None


def test_a_flat_block_list_is_read_too():
    """Where dovi_tool nests these depends on the RPU's content-mapping version.

    Looking in only the nested position would report a file that has a level 5
    block as having none, which is the exact silence this module exists to
    avoid.
    """
    flat = {"vdr_dm_data": {"ext_metadata_blocks": [{"Level5": _offsets(0, 0, 276, 276)}]}}

    assert rpu.read_active_area(flat) == rpu.ActiveArea(0, 0, 276, 276)


def test_a_level_5_block_that_cannot_be_read_is_an_error_not_a_no():
    """ "There is no level 5 here" and "I do not understand this" are opposite answers.

    Collapsing them into ``None`` is how a parser comes to report a film whose
    shape changes as a film carrying no metadata at all -- and the two lead to
    opposite decisions about whether the source can be cropped.
    """
    truncated = {
        "vdr_dm_data": {"ext_metadata_blocks": [{"Level5": {"active_area_top_offset": 276}}]}
    }

    with pytest.raises(rpu.ActiveAreaError):
        rpu.read_active_area(truncated)


def test_offsets_that_are_not_numbers_are_an_error():
    nonsense = {"vdr_dm_data": {"ext_metadata_blocks": [{"Level5": _offsets(0, 0, "N/A", 276)}]}}

    with pytest.raises(rpu.ActiveAreaError):
        rpu.read_active_area(nonsense)


def test_a_payload_with_no_metadata_at_all_reads_as_nothing():
    assert rpu.read_active_area({}) is None
    assert rpu.read_active_area({"vdr_dm_data": None}) is None


# --------------------------------------------------------------------------
# What the area means
# --------------------------------------------------------------------------


def test_the_area_says_what_is_left_of_the_frame():
    area = rpu.ActiveArea(0, 0, 276, 276)

    assert area.size_within(3840, 2160) == (3840, 1608)


def test_the_crop_is_in_the_order_a_project_file_writes_it():
    """`crop = [left, right, top, bottom]`, so this cannot be reordered quietly.

    A transposed pair looks entirely plausible -- 276 and 276 here are equal --
    and would cut the sides off a scope film instead of the bars.
    """
    assert rpu.ActiveArea(1, 2, 3, 4).as_crop() == (1, 2, 3, 4)


def test_all_zero_offsets_are_the_whole_frame():
    assert rpu.ActiveArea(0, 0, 0, 0).is_whole_frame
    assert not rpu.ActiveArea(0, 0, 276, 276).is_whole_frame


# --------------------------------------------------------------------------
# What several readings mean together
# --------------------------------------------------------------------------


def test_one_shape_at_every_position_is_a_fixed_shape():
    reading = rpu.Reading(shapes=(rpu.ActiveArea(0, 0, 276, 276),), positions=5, carrying=5)

    assert reading.fixed == rpu.ActiveArea(0, 0, 276, 276)
    assert not reading.varies
    assert not reading.absent


def test_two_shapes_are_a_film_that_changes_shape():
    """The case a single reading cannot see, and the reason there are five.

    Measured elsewhere on a real remux: The Dark Knight holds one shape for
    179,353 frames and another for 39,602, and a sample taken inside either
    stretch looks perfectly constant.
    """
    scope = rpu.read_active_area(REMUX_SCOPE)
    opened = rpu.read_active_area(REMUX_OPENED_OUT)
    assert scope != opened, "the two captured payloads must really differ"

    reading = rpu.Reading(shapes=(scope, opened), positions=5, carrying=5)

    assert reading.varies
    assert reading.fixed is None


def test_no_level_5_anywhere_is_absent_rather_than_varying():
    reading = rpu.Reading(shapes=(), positions=5, carrying=0)

    assert reading.absent
    assert not reading.varies
    assert reading.fixed is None


def test_level_5_at_only_some_positions_counts_as_changing_shape():
    """No block means no masking, which is a shape rather than missing data.

    Reporting this as a fixed shape would suggest a crop for a film that only
    wears it part of the time.
    """
    reading = rpu.Reading(shapes=(rpu.ActiveArea(0, 0, 276, 276),), positions=5, carrying=3)

    assert reading.varies
    assert reading.fixed is None
    assert not reading.absent


# --------------------------------------------------------------------------
# Where to read
# --------------------------------------------------------------------------


def test_positions_are_spread_across_the_film_and_stay_off_both_ends():
    """Opening logos and closing credits are often full-frame on a scope film.

    Sampling them would report a shape change that no viewer of the film proper
    would ever see.
    """
    seconds = rpu.positions_across(7800.0, positions=5)

    assert len(seconds) == 5
    assert seconds == sorted(seconds)
    assert seconds[0] > 7800.0 * 0.05
    assert seconds[-1] < 7800.0 * 0.95


def test_a_single_position_lands_in_the_middle():
    (only,) = rpu.positions_across(1000.0, positions=1)

    assert 400 < only < 600


def test_a_film_with_no_known_duration_is_read_at_its_start():
    assert rpu.positions_across(0.0) == [0.0]


def _offsets(left, right, top, bottom) -> dict:
    return {
        "active_area_left_offset": left,
        "active_area_right_offset": right,
        "active_area_top_offset": top,
        "active_area_bottom_offset": bottom,
    }


# --------------------------------------------------------------------------
# Getting the document out of dovi_tool's output
# --------------------------------------------------------------------------


def test_the_document_is_found_after_the_tool_s_own_chatter():
    """dovi_tool prints a progress line on standard output, ahead of the JSON.

    So the output is not JSON from its first character, and parsing the whole
    of it fails against every file there is -- including a perfectly good one.
    """
    payload = rpu.payload_from(
        'Parsing RPU file...\n{"vdr_dm_data": {"ext_metadata_blocks": []}}\n', "a source"
    )

    assert payload == {"vdr_dm_data": {"ext_metadata_blocks": []}}


def test_output_with_no_document_in_it_is_an_error():
    with pytest.raises(rpu.ActiveAreaError, match="no RPU document"):
        rpu.payload_from("Parsing RPU file...\nError: invalid RPU\n", "a source")


def test_a_document_that_will_not_parse_is_an_error():
    with pytest.raises(rpu.ActiveAreaError, match="did not parse as JSON"):
        rpu.payload_from('Parsing RPU file...\n{"vdr_dm_data": ', "a source")
