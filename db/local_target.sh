#!/bin/sh
# "Is the server this DSN names local?" — the ONE implementation of that question
# (#984; #990 review).
#
# Two commands in this repository write to a Postgres SERVER rather than only to the
# database they were pointed at:
#
#   * db/reset_database.sh drops and recreates the target, and
#   * tools/schema_drift.py creates a reference database beside the target and
#     force-drops it again once it has diffed the two catalogs.
#
# Both must refuse a host that is not this machine unless the operator says so out loud,
# and "which hosts count as this machine" has to have exactly one answer. A second copy
# of that list is the defect class #984 exists to remove — one of the seven appliers
# differed from the others by less — so the list lives here and both callers run this
# file. They keep their own override variables on purpose: authorising a scratch
# database beside staging is not the same decision as authorising `drop database` on it.
#
# The DSN arrives on STDIN, never in argv. A DATABASE_URL carries a password and argv is
# world-readable on Linux (/proc/<pid>/cmdline); an environment variable would be a
# smaller hole and stdin is none.
#
# Usage:
#   printf '%s' "$dsn" | sh db/local_target.sh
#
# Prints one `name='value'` assignment per line, single-quoted so that `eval` is safe in
# shell and `shlex` reads it in Python:
#
#   ta_scheme ta_authority ta_database ta_query ta_host ta_redacted ta_locality
#
# `ta_redacted` has the password removed and is the ONLY form safe to print or log.
# `ta_locality` is `local` or `remote`.
# Exit 0 when the DSN parsed, 2 when it is not a postgresql:// URI.
set -eu

dsn="$(cat)"

case "$dsn" in
    postgresql://* | postgres://*) ;;
    *)
        # The DSN itself is NOT echoed: a key=value conninfo carries `password=...` in
        # plain sight, and this message reaches a terminal and a CI log.
        echo "local_target.sh: expected a postgresql:// or postgres:// URI (a key=value conninfo is not one)" >&2
        exit 2
        ;;
esac

# Only the database name is ever moved by a caller (to `postgres`, the maintenance
# database it connects to in order to create or drop); everything else — user, password,
# host, port, query parameters — is carried through untouched, so an admin connection is
# the same connection with a different dbname.
scheme="${dsn%%://*}"
rest="${dsn#*://}"
query=""
case "$dsn" in *\?*) query="?${dsn#*\?}" ;; esac
rest="${rest%%\?*}"
authority="${rest%%/*}"
case "$rest" in
    */*) database="${rest#*/}" ;;
    *) database="" ;;
esac
hostport="${authority##*@}"
case "$hostport" in
    \[*) host="${hostport%%\]*}]" ;;
    *) host="${hostport%%:*}" ;;
esac

# The same URI with the password removed — the only form a caller may print. A URI
# encodes `@` and `:` inside a password as %40/%3A, so splitting the userinfo on the
# first `:` cannot cut a password in half and leave part of it in the output.
case "$authority" in
    *@*)
        userinfo="${authority%%@*}"
        redacted="${scheme}://${userinfo%%:*}@${hostport}"
        ;;
    *)
        redacted="${scheme}://${hostport}"
        ;;
esac
redacted="${redacted}/${database}"

# An empty host is libpq's default, which is a Unix socket on this machine — that is how
# the compose container's own psql and the postgres image's initdb hook connect, and an
# explicit leading `/` is a socket directory. Everything else is another machine until
# proven otherwise, a hostname that happens to resolve to a loopback address included:
# this has to be decidable without a DNS lookup, and "it resolved to 127.0.0.1 today" is
# not a property a drop-database guard should rest on.
case "$host" in
    '' | localhost | 127.0.0.1 | '[::1]' | ::1 | /*) locality=local ;;
    *) locality=remote ;;
esac

# POSIX single-quote escaping, in the shell rather than through sed: a value reaching a
# caller's `eval` must not be able to end its own quoting. A URI cannot legally contain a
# bare apostrophe (it would be %27), which is exactly why this is here — the guard has to
# hold for the input nobody expected.
emit() {
    emit_value="$2"
    emit_out=""
    while :; do
        case "$emit_value" in
            *\'*)
                emit_out="${emit_out}${emit_value%%\'*}'\\''"
                emit_value="${emit_value#*\'}"
                ;;
            *)
                emit_out="${emit_out}${emit_value}"
                break
                ;;
        esac
    done
    printf "%s='%s'\n" "$1" "$emit_out"
}

emit ta_scheme "$scheme"
emit ta_authority "$authority"
emit ta_database "$database"
emit ta_query "$query"
emit ta_host "$host"
emit ta_redacted "$redacted"
emit ta_locality "$locality"
