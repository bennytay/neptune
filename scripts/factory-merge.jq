# Pure JSON filters for scripts/factory-merge.sh, kept apart so tests run them on fixture JSON
# without the network: `jq -L scripts 'include "factory-merge"; ...'`.

# Only these GitHub author associations may cast a review verdict.
def trusted_author: IN("OWNER", "MEMBER", "COLLABORATOR");

# The lines of a comment body, HTML comments (`<!-- -->`, or an unclosed `<!--` to the end) removed,
# that are neither quoted (`>`) nor inside a ``` or ~~~ fence, nor indented as a code block: a
# verdict that is hidden or quoted from elsewhere must not count.
def _live_lines:
  reduce (gsub("<!--[\\s\\S]*?(-->|\\z)"; "") | split("\n")[] | sub("\r$"; "")) as $line ({fence: null, out: []};
    ([$line | capture("^ {0,3}(?<f>`{3,}|~{3,})") | .f] | first) as $mark
    | if .fence != null then
        (if $mark != null and ($mark[:1] == .fence[:1]) and ($mark | length) >= (.fence | length)
         then .fence = null else . end)
      elif $mark != null then .fence = $mark
      elif ($line | test("^ {0,3}>") or test("^( {4}|\t)")) then .
      else .out += [$line] end)
  | .out[];

# Input: an array of {at, association, body} built from the PR's issue comments and review bodies.
# Output: the verdict (upper-cased, e.g. MERGE or REVISE) of the latest `Review: <VERDICT> @ <sha>`
# line by a trusted author whose SHA (7-40 hex digits, ending at a non-alphanumeric) prefixes $head,
# or nothing. A later verdict overrides an earlier one, so a REVISE posted after a MERGE blocks the
# merge. Bold or italic emphasis around the line is tolerated; hidden (HTML comment), quoted,
# fenced, indented or inline-code lines are ignored.
def verdict($head):
  [ .[]
    | select(.association | trusted_author)
    | .at as $at
    | (.body // "") | _live_lines
    | capture("^ {0,3}[*_]*Review:[\\s*_]*(?<v>[A-Za-z_-]+)[\\s*_]*@[\\s*_]*(?<sha>[0-9a-fA-F]{7,40})(?![0-9A-Za-z])")
    | select(.sha as $s | $head | startswith($s | ascii_downcase))
    | {at: $at, v: (.v | ascii_upcase)} ]
  | sort_by(.at) | last | .v // empty;

# A line that ends the **Architecture change** section: another bold heading, an HTML block or a
# markdown heading.
def _section_end: test("^[\\s>]*(\\*\\*[^*]+\\*\\*|</?details|<summary|#{1,6}\\s)");

# A line with content once a list marker and an empty `Added:` / `Changed:` / `Removed:` label are
# stripped, so the template's bare skeleton does not count as filled.
def _filled: sub("^[\\s>]*([-*+]|[0-9]+\\.)?\\s*((Added|Changed|Removed)\\s*:)?"; "") | test("\\S");

# Input: the pull request JSON. True when its body, HTML comments removed, has an
# **Architecture change** section with at least one line of content (the heading's own line counts).
def architecture_change_filled:
  [ (.body // "") | gsub("<!--[\\s\\S]*?-->"; "") | split("\n")[] | sub("\r$"; "") ] as $lines
  | ([ range(0; $lines | length) | select($lines[.] | test("^[\\s>]*\\*\\*Architecture change\\*\\*")) ]
     | first) as $i
  | if $i == null then false
    else
      ($lines[$i + 1:] | (map(_section_end) | index(true)) as $j | if $j == null then . else .[:$j] end)
      as $rest
      | [ ($lines[$i] | sub("^[\\s>]*\\*\\*Architecture change\\*\\*[\\s:]*"; "")) ] + $rest
      | any(.[]; _filled)
    end;
