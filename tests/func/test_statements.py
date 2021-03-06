from contextlib import contextmanager
import json
import re
import time

import pytest

from temboardagent.spc import connector

from test.temboard import temboard_request, urllib2

ENV = {}

@pytest.fixture(scope="module")
def xsession(env):
    ENV.update(env)
    conn = connector(
        host=ENV["pg"]["socket_dir"],
        port=ENV["pg"]["port"],
        user=ENV["pg"]["user"],
        password=ENV["pg"]["password"],
        database="postgres",
    )
    conn.connect()
    conn.close()
    status, res = temboard_request(
        ENV["agent"]["ssl_cert_file"],
        method="POST",
        url="https://{host}:{port}/login".format(**ENV["agent"]),
        headers={"Content-type": "application/json"},
        data={
            "username": ENV["agent"]["user"],
            "password": ENV["agent"]["password"],
        },
    )
    assert status == 200
    return json.loads(res)["session"]


@contextmanager
def conn():
    cnx = connector(
        host=ENV["pg"]["socket_dir"],
        port=ENV["pg"]["port"],
        user=ENV["pg"]["user"],
        password=ENV["pg"]["password"],
        database="postgres",
    )
    cnx.connect()
    try:
        yield cnx
    finally:
        cnx.close()


@pytest.fixture(scope="function")
def extension_enabled(env):
    ENV.update(env)
    with conn() as cnx:
        cnx.execute("CREATE EXTENSION pg_stat_statements")
    yield
    with conn() as cnx:
        cnx.execute("DROP EXTENSION pg_stat_statements")


def test_statements_not_enabled(xsession):
    try:
        status, res = temboard_request(
            ENV["agent"]["ssl_cert_file"],
            method="GET",
            url="https://{host}:{port}/statements".format(**ENV["agent"]),
            headers={"X-Session": xsession},
        )
    except urllib2.HTTPError as e:
        status = e.code
    assert status == 404


def test_statements(xsession, extension_enabled):
    with conn() as cnx:
        cnx.execute("SELECT version()")
        row, = cnx.get_rows()
        m = re.match(r"PostgreSQL (\d+\.?\d*)", row["version"])
        assert m
        pg_version = tuple(
            int(x) for x in m.group(1).split(".")
        )

    def get_statements():
        try:
            status, res = temboard_request(
                ENV["agent"]["ssl_cert_file"],
                method="GET",
                url="https://{host}:{port}/statements".format(**ENV["agent"]),
                headers={"X-Session": xsession},
            )
        except urllib2.HTTPError as e:
            status = e.code
        assert status == 200
        return json.loads(res)

    result = get_statements()
    snapshot_datetime = time.strptime(
        result["snapshot_datetime"], "%Y-%m-%d %H:%M:%S +0000"
    )
    data = result["data"]
    assert data
    expected_keys = set(
        [
            u"blk_read_time",
            u"blk_write_time",
            u"calls",
            u"datname",
            u"dbid",
            u"local_blks_dirtied",
            u"local_blks_hit",
            u"local_blks_read",
            u"local_blks_written",
            u"query",
            u"queryid",
            u"rolname",
            u"rows",
            u"shared_blks_dirtied",
            u"shared_blks_hit",
            u"shared_blks_read",
            u"shared_blks_written",
            u"temp_blks_read",
            u"temp_blks_written",
            u"userid",
        ]
    )
    if pg_version >= (13,):
        expected_keys = expected_keys | set([
            u'max_exec_time',
            u'max_plan_time',
            u'mean_exec_time',
            u'mean_plan_time',
            u'min_exec_time',
            u'min_plan_time',
            u'plans',
            u'rolname',
            u'rows',
            u'stddev_exec_time',
            u'stddev_plan_time',
            u'total_exec_time',
            u'total_plan_time',
            u'wal_bytes',
            u'wal_fpi',
            u'wal_records',
        ])
    else:
        expected_keys = expected_keys | set([
            u"max_time",
            u"mean_time",
            u"min_time",
            u"stddev_time",
            u"total_time",
        ])

    assert all(set(d) == expected_keys for d in data)
    assert "temboard" in set([d["rolname"] for d in data])
    assert "postgres" in set([d["datname"] for d in data])
    queries = [d["query"] for d in data]
    assert "CREATE EXTENSION pg_stat_statements" in queries

    query = "SELECT 1+1"
    with conn() as cnx:
        cnx.execute(query)

    # sleep 1s to get a different snapshot timestamp
    time.sleep(1)

    result = get_statements()
    new_snapshot_datetime = time.strptime(
        result["snapshot_datetime"], "%Y-%m-%d %H:%M:%S +0000"
    )
    assert new_snapshot_datetime > snapshot_datetime
    new_data = result["data"]
    assert new_data
    assert all(set(d) == expected_keys for d in new_data)
    assert len(new_data) != len(data)
    assert "temboard" in set([d["rolname"] for d in new_data])
    assert "postgres" in set([d["datname"] for d in new_data])
    new_queries = [d["query"] for d in new_data]

    stmt_query = "SELECT $1+$2" if pg_version > (10,) else "SELECT ?+?"
    assert stmt_query in new_queries
    calls = [d["calls"] for d in new_data if d["query"] == stmt_query][0]
    assert calls == 1

    with conn() as cnx:
        cnx.execute(query)

    # sleep 1s to get a different snapshot timestamp
    time.sleep(1)

    result = get_statements()
    third_data = result["data"]
    calls = [d["calls"] for d in third_data if d["query"] == stmt_query][0]
    assert calls == 2
