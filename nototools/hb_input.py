# Copyright 2016 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


from __future__ import division, print_function

from fontTools.ttLib import TTFont
from nototools import summary


class HbInputGenerator(object):
    """Provides functions to generate harbuzz input.

    The input is returned as a list of strings, suitable for passing into
    subprocess.call or something similar.
    """

    def __init__(self, font):
        self.font = font
        self.reverse_cmap = build_reverse_cmap(self.font)

        self.widths = {}
        glyph_set = font.getGlyphSet()
        for name in glyph_set.keys():
            glyph = glyph_set[name]
            if glyph.width:
                width = glyph.width
            elif hasattr(glyph._glyph, 'xMax'):
                width = abs(glyph._glyph.xMax - glyph._glyph.xMin)
            else:
                width = 0
            self.widths[name] = width
        space_name = font['cmap'].tables[0].cmap[0x0020]
        self.widths['space'] = self.widths[space_name]

    def all_inputs(self, warn=False):
        """Generate harfbuzz inputs for all glyphs in a given font."""

        inputs = []
        glyph_set = self.font.getGlyphSet()
        for name in self.font.getGlyphOrder():
            is_zero_width = glyph_set[name].width == 0
            cur_input = self.input_from_name(name, pad=is_zero_width)
            if cur_input is not None:
                inputs.append(cur_input)
            elif warn:
                print('not tested (unreachable?): %s' % name)
        return inputs

    def input_from_name(self, name, features=(), seen=None, pad=False):
        """Given glyph name, return input to harbuzz to render this glyph.

        Returns input in the form of a (features, text) tuple, where `features`
        is a list of feature tags to activate and `text` is an input string.

        Argument `features` will simply be passed through to the output, and is
        used for recursive calls to the method. `seen` is used by the method to
        avoid following cycles when recursively looking for possible input.
        `pad` can be used to add whitespace to text output, for non-spacing
        glyphs.

        Can return None in two situations: if no possible input is found (no
        simple unicode mapping or substitution rule exists to generate the
        glyph), or if the requested glyph already exists in `seen` (in which
        case this path of generating input should not be followed further).
        """

        inputs = []

        # avoid following cyclic paths through features
        if seen is None:
            seen = set()
        if name in seen:
            return None
        seen.add(name)

        # see if this glyph has a simple unicode mapping
        if name in self.reverse_cmap:
            text = unichr(self.reverse_cmap[name])
            inputs.append((features, text))

        # check the substitution features
        inputs.extend(self._inputs_from_gsub(name, features, seen))
        seen.remove(name)

        # since this method sometimes returns None to avoid cycles, the
        # recursive calls that it makes might have themselves returned None,
        # but we should avoid returning None here if there are other options
        inputs = [i for i in inputs if i is not None]
        if not inputs:
            return None
        features, text = min(inputs)

        if pad:
            width, space = self.widths[name], self.widths['space']
            text = ' ' * (width // space + (1 if width % space else 0)) + text
        return features, text

    def _inputs_from_gsub(self, name, features, seen):
        """Check GSUB for possible input yielding glyph with given name.
        The `features` and `seen` arguments are passed in from the original call
        to input_from_name().
        """

        inputs = []
        if 'GSUB' not in self.font:
            return inputs
        gsub = self.font['GSUB'].table
        if gsub.LookupList is None:
            return inputs
        for lookup_index, lookup in enumerate(gsub.LookupList.Lookup):
            for st in lookup.SubTable:

                # see if this glyph can be a single-glyph substitution
                if lookup.LookupType == 1:
                    for glyph, subst in st.mapping.items():
                        if subst == name:
                            inputs.append(self._input_with_context(
                                gsub, [glyph], lookup_index, features, seen))

                # see if this glyph is a ligature
                elif lookup.LookupType == 4:
                    for prefix, ligatures in st.ligatures.items():
                        for ligature in ligatures:
                            if ligature.LigGlyph == name:
                                glyphs = [prefix] + list(ligature.Component)
                                inputs.append(self._input_with_context(
                                    gsub, glyphs, lookup_index, features, seen))
        return inputs

    def _input_with_context(self, gsub, glyphs, target_index, features, seen):
        """Given GSUB, input glyphs, and target lookup index, return input to
        harfbuzz to render the input glyphs with the target lookup activated.
        """

        inputs = []

        # try to get a feature tag to activate this lookup
        for feature in gsub.FeatureList.FeatureRecord:
            if target_index in feature.Feature.LookupListIndex:
                features += (feature.FeatureTag,)
                inputs.append(self._sequence_from_glyph_names(
                    glyphs, features, seen))

        # try for a chaining substitution
        for lookup_index, lookup in enumerate(gsub.LookupList.Lookup):
            if lookup.LookupType == 6:
                for st in lookup.SubTable:
                    input_glyphs = [min(c.glyphs) for c in st.InputCoverage]
                    if not (any(
                        subst_lookup.LookupListIndex == target_index
                        for subst_lookup in st.SubstLookupRecord) and
                        self._is_sublist(input_glyphs, glyphs)):
                        continue
                    if st.LookAheadCoverage:
                        la = [min(c.glyphs) for c in st.LookAheadCoverage]
                        input_glyphs = input_glyphs + la
                    if st.BacktrackCoverage:
                        bt = list(reversed([min(c.glyphs)
                                            for c in st.BacktrackCoverage]))
                        input_glyphs = bt + input_glyphs
                    inputs.append(self._input_with_context(
                        gsub, input_glyphs, lookup_index, features, seen))

        inputs = [i for i in inputs if i is not None]
        return min(inputs) if inputs else None

    def _sequence_from_glyph_names(self, glyphs, features, seen):
        """Return a sequence of glyphs from glyph names."""

        text = []
        for glyph in glyphs:
            cur_input = self.input_from_name(glyph, features, seen)
            if cur_input is None:
                return None
            features, cur_text = cur_input
            text.append(cur_text)
        return features, ''.join(text)

    def _is_sublist(self, lst, sub):
        """Return whether sub is a sub-list of lst."""

        return any(lst[i:i + len(sub)] == sub
                   for i in range(1 + len(lst) - len(sub)))


def build_reverse_cmap(font):
    """Build a dictionary mapping glyph names to unicode values.
    Maps each name to its smallest unicode value.
    """

    cmap_items = summary.get_largest_cmap(font).items()
    return {n: v for v, n in reversed(sorted(cmap_items))}
