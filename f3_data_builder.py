#!/usr/bin/env python

import os
import logging
import ast
from typing import Any, Tuple, Hashable

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, MetaData, Table, literal_column
from sqlalchemy.sql import select, func, or_
from sqlalchemy.sql.expression import Insert, Subquery, Selectable
from sqlalchemy.engine import Engine
from sqlalchemy.dialects.mysql import insert
from pandas._libs.missing import NAType


def mysql_connection() -> Engine:
    """Connect to MySQL. This involves loading environment variables from file"""
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    load_dotenv()
    engine = create_engine(
        f"mysql+mysqlconnector://{os.getenv('DATABASE_USER')}:{os.getenv('DATABASE_PASSWORD')}@{os.getenv('DATABASE_HOST')}:3306"
    )
    return engine


def insert_statement(table: Table, insert_values: list[dict[Hashable, Any]], update_cols: Tuple[str, ...]) -> Insert:
    """
    Abstract the MySQL insert statement. Returns a SQLAlchemy INSERT statement that renders
    the following:

    ```sql
    INSERT INTO <table> (col1, col2, ...) VALUES ((val1a, val1b, ...), (val2a, val2b, ...), ...)
    AS NEW ON DUPLICATE KEY UPDATE colx = NEW(colx), coly = NEW(coly), ...
    ```

    In this way, the creation and execution of MySQL INSERT statements becomes far less error prone and more
    standardized.

    :param table: The target table for the INSERT statement
    :type table: SQLAlchemy Table object
    :param insert_values: A list of dictionaries. Each dictionary is a key / value pair preresenting
    the table column_name / value_to_insert.
    :type insert_values: list[dict[str, Any]]
    :rtype: Insert[Any]
    :return: SQLAlchemy INSERT statement
    """
    sql = insert(table).values(insert_values)
    on_dup = sql.on_duplicate_key_update({v.name: v for v in sql.inserted if v.name in update_cols})
    return on_dup


def region_subquery(metadata: MetaData) -> Subquery:
    """
    Abstracting some SQL duplication between the
    paxminer and weaselbot region queries
    """
    cb = metadata.tables["weaselbot.combined_beatdowns"]
    a = metadata.tables["weaselbot.combined_aos"]

    sql = select(
        a.c.region_id,
        func.max(cb.c.timestamp).label("max_timestamp"),
        func.max(cb.c.ts_edited).label("max_ts_edited"),
        func.count().label("beatdown_count"),
    )
    sql = sql.select_from(cb.join(a, cb.c.ao_id == a.c.ao_id))
    sql = sql.group_by(a.c.region_id).subquery("b")
    return sql


def paxminer_region_query(metadata: MetaData, cr: Table) -> Selectable:
    """
    Construct the region SQL using paxminer
    """
    r = metadata.tables["paxminer.regions"]
    sub = region_subquery(metadata)

    sql = select(
        r.c.schema_name,
        r.c.region.label("region_name"),
        sub.c.max_timestamp,
        sub.c.max_ts_edited,
        sub.c.beatdown_count,
        cr.c.region_id,
    )
    sql = sql.select_from(
        r.outerjoin(cr, r.c.schema_name == cr.c.schema_name).outerjoin(sub, cr.c.region_id == sub.c.region_id)
    )

    return sql


def weaselbot_region_query(metadata: MetaData, cr: Table) -> Selectable:
    """
    Construct the region SQL using weaselbot
    """
    sub = region_subquery(metadata)

    sql = select(cr, sub.c.beatdown_count)
    sql = sql.select_from(cr.outerjoin(sub, cr.c.region_id == sub.c.region_id))

    return sql


def region_queries(engine: Engine, metadata: MetaData) -> pd.DataFrame:
    """
    Using PAXMiner and Weaselbot region tables, make updates to the
    Weaselbot combined_regions table if any exist from PAXMiner.

    :param engine: SQLAlchemy connection engine to MySQL
    :type engine: sqlalchemy.engine.Engine object
    :param metadata: collection of reflected table metadata
    :type metadata: SQLAlchemy MetaData
    :rtype: pandas.DataFrame
    :return: A dataframe containing current region information
    """
    cr = metadata.tables["weaselbot.combined_regions"]

    paxminer_region_sql = paxminer_region_query(metadata, cr)

    df_regions = pd.read_sql(paxminer_region_sql, engine)
    insert_values = df_regions.to_dict("records")
    update_cols = ("region_name", "max_timestamp", "max_ts_edited")
    region_insert_sql = insert_statement(cr, insert_values, update_cols)

    with engine.begin() as cnxn:
        cnxn.execute(region_insert_sql)

    dtypes = dict(
        region_id=pd.StringDtype(),
        region_name=pd.StringDtype(),
        schema_name=pd.StringDtype(),
        slack_team_id=pd.StringDtype(),
        max_timestamp=pd.Float64Dtype(),
        max_ts_edited=pd.Float64Dtype(),
        beatdown_count=pd.Int16Dtype(),
    )

    weaselbot_region_sql = weaselbot_region_query(metadata, cr)
    df_regions = pd.read_sql(weaselbot_region_sql, engine, dtype=dtypes)

    return df_regions


def pull_users(row: tuple[Any, ...], engine: Engine, metadata: MetaData) -> pd.DataFrame:
    dtypes = dict(
        slack_user_id=pd.StringDtype(), user_name=pd.StringDtype(), email=pd.StringDtype(), region_id=pd.StringDtype()
    )
    try:
        usr = Table("users", metadata, autoload_with=engine, schema=row.schema_name)
    except Exception as e:
        logging.error(f"{e}")
        return pd.DataFrame(columns=dtypes.keys())
    
    sql = select(
        usr.c.user_id.label("slack_user_id"),
        usr.c.user_name,
        usr.c.email,
        literal_column(f"'{row.region_id}'").label("region_id"),
    )

    with engine.begin() as cnxn:
        df = pd.read_sql(sql, cnxn, dtype=dtypes)

    return df


def pull_aos(row: tuple[Any, ...], engine: Engine, metadata: MetaData) -> pd.DataFrame:
    dtypes = dict(slack_channel_id=pd.StringDtype(), ao_name=pd.StringDtype(), region_id=pd.StringDtype())
    try:
        ao = Table("aos", metadata, autoload_with=engine, schema=row.schema_name)
    except Exception as e:
        logging.error(e)
        return pd.Datarame(columns=dtypes.keys())
    
    sql = select(
        ao.c.channel_id.label("slack_channel_id"),
        ao.c.ao.label("ao_name"),
        literal_column(f"'{row.region_id}'").label("region_id"),
    )
    with engine.begin() as cnxn:
        df = pd.read_sql(sql, cnxn, dtype=dtypes)

    return df

def pull_beatdowns(row: tuple[Any, ...], engine: Engine, metadata: MetaData) -> pd.DataFrame:
    dtypes = dict(
        slack_channel_id=pd.StringDtype(),
        slack_q_user_id=pd.StringDtype(),
        slack_coq_user_id=pd.StringDtype(),
        pax_count=pd.Int16Dtype(),
        fng_count=pd.Int16Dtype(),
        region_id=pd.StringDtype(),
        timestamp=pd.Float64Dtype(),
        ts_edited=pd.StringDtype(),
        backblast=pd.StringDtype(),
        json=pd.StringDtype(),
    )
    try:
        beatdowns = Table("beatdowns", metadata, autoload_with=engine, schema=row.schema_name)
    except Exception as e:
        logging.error(e)
        return pd.DataFrame(columns=dtypes.keys())
    
    sql = select(
                beatdowns.c.ao_id.label("slack_channel_id"),
                beatdowns.c.bd_date,
                beatdowns.c.q_user_id.label("slack_q_user_id"),
                beatdowns.c.coq_user_id.label("slack_coq_user_id"),
                beatdowns.c.pax_count,
                beatdowns.c.fng_count,
                literal_column(f"'{row.region_id}'").label("region_id"),
                beatdowns.c.timestamp,
                beatdowns.c.ts_edited,
                beatdowns.c.backblast,
                beatdowns.c.json,
            )
    
    if all([not isinstance(x, type(pd.NA)) for x in (row.max_timestamp, row.max_ts_edited)]):
        sql = sql.where(or_(beatdowns.c.timestamp > str(row.max_timestamp), beatdowns.c.ts_edited > str(row.max_ts_edited)))
    elif not isinstance(row.max_timestamp, type(pd.NA)):
        sql = sql.where(beatdowns.c.timestamp > str(row.max_timestamp))

    with engine.begin() as cnxn:
        df = pd.read_sql(sql, cnxn, dtype=dtypes)
    df["json"] = df["json"].str.replace("'", '"') # converting the string object to proper JSON 

    return df

def pull_attendance(row: tuple[Any, ...], engine: Engine, metadata: MetaData) -> pd.DataFrame:
    dtypes = dict(
        slack_channel_id=pd.StringDtype(),
        slack_q_user_id=pd.StringDtype(),
        slack_user_id=pd.StringDtype(),
        region_id=pd.StringDtype(),
        json=pd.StringDtype(),
    )

    try:
        attendance = Table("bd_attendance", metadata, autoload_with=engine, schema=row.schema_name)
    except Exception as e:
        logging.error(e)
        return pd.DataFrame(columns=dtypes.keys())
    
    sql = select(
                attendance.c.ao_id.label("slack_channel_id"),
                attendance.c.date.label("bd_date"),
                attendance.c.q_user_id.label("slack_q_user_id"),
                attendance.c.user_id.label("slack_user_id"),
                literal_column(f"'{row.region_id}'").label("region_id"),
                attendance.c.json,
            )
    if all([not isinstance(x, type(pd.NA)) for x in (row.max_timestamp, row.max_ts_edited)]):
        sql = sql.where(or_(attendance.c.timestamp > str(row.max_timestamp), attendance.c.ts_edited > str(row.max_ts_edited)))
    elif not isinstance(row.max_timestamp, type(pd.NA)):
        sql = sql.where(attendance.c.timestamp > str(row.max_timestamp))

    with engine.begin() as cnxn:
        df = pd.read_sql(sql, cnxn, dtype=dtypes)

    return df


def build_users(
    df_users_dup: pd.DataFrame, df_attendance: pd.DataFrame, engine: Engine, metadata: MetaData
) -> pd.DataFrame:
    """
    Process the user information from each region. Attendance information is taken into account and
    inserted/updated in each target table accordingly. Returns a pandas DataFrame that updates the
    input dataframe `df_users_dup`

    :param df_users_dup: pandas DataFrame object containing each region's user info
    :type df_users_dup: pandas.DataFrame object
    :param df_attendance: pandas DataFrame object containing each region's attendance information
    :type df_attendance: pandas.DataFrame object
    :param engine: SQLAlchemy connection engine to MySQL
    :type engine: sqlalchemy.engine.Engine object
    :param metadata: collection of reflected table metadata
    :type metadata: SQLAlchemy MetaData
    :rtype: pandas.DataFrame
    :return: updated df_users_dup dataframe
    """

    logging.info("building users...")

    cu = metadata.tables["weaselbot.combined_users"]
    cud = metadata.tables["weaselbot.combined_users_dup"]

    df_users_dup["email"] = df_users_dup["email"].str.lower()
    df_users_dup = df_users_dup[df_users_dup["email"].notna()]

    df_user_agg = (
        df_attendance.groupby(["slack_user_id"], as_index=False)["bd_date"].count().rename({"bd_date": "count"}, axis=1)
    )
    df_users = (
        df_users_dup.merge(df_user_agg[["slack_user_id", "count"]], on="slack_user_id", how="left")
        .fillna(0)
        .sort_values(by="count", ascending=False)
    )

    df_users.drop_duplicates(subset=["email"], keep="first", inplace=True)

    insert_values = (
        df_users[["user_name", "email", "region_id"]].rename({"region_id": "home_region_id"}, axis=1).to_dict("records")
    )

    for d in insert_values:
        try:
            d["home_region_id"] = int(d["home_region_id"])
        except TypeError:
            pass

    update_cols = ("user_name", "email", "home_region_id")
    user_insert_sql = insert_statement(cu, insert_values, update_cols)

    with engine.begin() as cnxn:
        cnxn.execute(user_insert_sql)

    dtypes = dict(
        user_id=pd.StringDtype(), user_name=pd.StringDtype(), email=pd.StringDtype(), home_region_id=pd.StringDtype()
    )

    df_users = pd.read_sql(select(cu), engine, dtype=dtypes)
    df_users_dup = df_users_dup.merge(df_users[["email", "user_id"]], on="email", how="left")

    insert_values = df_users_dup[["slack_user_id", "user_name", "email", "region_id", "user_id"]].to_dict("records")

    for d in insert_values:
        try:
            d["user_id"] = int(d["user_id"])
        except TypeError:
            pass  # allowing NA to flow through
        try:
            d["region_id"] = int(d["region_id"])
        except TypeError:
            pass

    update_cols = ("user_name", "email", "region_id", "user_id")
    user_dup_insert_sql = insert_statement(cud, insert_values, update_cols)

    with engine.begin() as cnxn:
        cnxn.execute(user_dup_insert_sql)

    return df_users_dup


def build_aos(df_aos: pd.DataFrame, engine: Engine, metadata: MetaData) -> pd.DataFrame:
    """
    Returns a pandas DataFrame that reflects an update to the input dataframe after
    table inserts/updates.

    :param df_aos: pandas DataFrame object containing each region's AO information
    :type df_aos: pandas.DataFrame object
    :param engine: SQLAlchemy connection engine to MySQL
    :type engine: sqlalchemy.engine.Engine object
    :param metadata: collection of reflected table metadata
    :type metadata: SQLAlchemy MetaData
    :rtype: pandas.DataFrame
    :return: updated df_aos dataframe
    """
    logging.info("building aos...")
    ca = metadata.tables["weaselbot.combined_aos"]
    insert_values = df_aos[["slack_channel_id", "ao_name", "region_id"]].to_dict("records")

    for d in insert_values:
        try:
            d["region_id"] = int(d["region_id"])
        except TypeError:
            pass

    update_cols = ("ao_name",)
    aos_insert_sql = insert_statement(ca, insert_values, update_cols)

    with engine.begin() as cnxn:
        cnxn.execute(aos_insert_sql)

    dtypes = {
        "ao_id": pd.StringDtype(),
        "slack_channel_id": pd.StringDtype(),
        "ao_name": pd.StringDtype(),
        "region_id": pd.StringDtype(),
    }

    return pd.read_sql(select(ca), engine, dtype=dtypes)


def extract_user_id(slack_user_id) -> NAType | str:
    """
    Process Slack user ID's. Some of these are
    not just simple user ID's. Clean them up
    to standardize across the process.

    :param slack_user_id: User ID from Slack
    :type slack_user_id: str
    :rtype: str | pandas.NA
    :return: cleaned userid string.
    """

    match isinstance(slack_user_id, type(pd.NA)):
        case True:
            return pd.NA
        case _:
            if slack_user_id.startswith("U"):
                return slack_user_id
            elif "team" in slack_user_id:
                return slack_user_id.split("/team/")[1].split("|")[0]


def build_beatdowns(
    df_beatdowns: pd.DataFrame, df_users_dup: pd.DataFrame, df_aos: pd.DataFrame, engine: Engine, metadata: MetaData
) -> pd.DataFrame:
    """
    Returns an updated beatdowns dataframe after updates/inserts to the weaselbot.combined_beatdowns table.

    :param df_beatdowns: pandas DataFrame object containing each region's beatdown information
    :type df_beatdowns: pandas.DataFrame object
    :param df_users_dup: pandas DataFrame object containing each region's users information
    :type df_users_dup: pandas.DataFrame object
    :param df_aos: pandas DataFrame object containing each region's AO information
    :type df_aos: pandas.DataFrame object
    :param engine: SQLAlchemy connection engine to MySQL
    :type engine: sqlalchemy.engine.Engine object
    :param metadata: collection of reflected table metadata
    :type metadata: SQLAlchemy MetaData
    :rtype: pandas.DataFrame
    :return: updated df_beatdowns dataframe
    """

    logging.info("building beatdowns...")
    df_beatdowns["slack_q_user_id"] = df_beatdowns["slack_q_user_id"].apply(extract_user_id).astype(pd.StringDtype())
    df_beatdowns["slack_coq_user_id"] = (
        df_beatdowns["slack_coq_user_id"].apply(extract_user_id).astype(pd.StringDtype())
    )

    cb = metadata.tables["weaselbot.combined_beatdowns"]

    # find duplicate slack_user_ids on df_users_dup
    df_beatdowns = (
        df_beatdowns.merge(
            df_users_dup[["slack_user_id", "user_id", "region_id"]],
            left_on=["slack_q_user_id", "region_id"],
            right_on=["slack_user_id", "region_id"],
            how="left",
        )
        .rename({"user_id": "q_user_id"}, axis=1)
        .merge(
            df_users_dup[["slack_user_id", "user_id", "region_id"]],
            left_on=["slack_coq_user_id", "region_id"],
            right_on=["slack_user_id", "region_id"],
            how="left",
        )
        .rename({"user_id": "coq_user_id"}, axis=1)
        .merge(
            df_aos[["slack_channel_id", "ao_id", "region_id"]],
            on=["slack_channel_id", "region_id"],
            how="left",
        )
    )
    df_beatdowns["fng_count"] = df_beatdowns["fng_count"].fillna(0)

    insert_values = df_beatdowns[df_beatdowns["ao_id"].notna()][
        [
            "ao_id",
            "bd_date",
            "q_user_id",
            "coq_user_id",
            "pax_count",
            "fng_count",
            "timestamp",
            "ts_edited",
            "backblast",
            "json",
        ]
    ].to_dict("records")

    # below columns are INT in their target table. coerce them so they'll load properly
    # leaving them as strings in the dataframes for later ease in merges/joins
    # NOTE: YHC is unable to test the JSON datatype. Presumbaly, MySQL will want those
    # sent over as proper dictionaries and not string representations of dictionaries.
    # This is the role of `ast.literal_eval`. If that's not the case, then just remove
    # the `if` statement logic to keep them as strings.
    for d in insert_values:
        for col in ("ao_id", "q_user_id", "coq_user_id"):
            try:
                d[col] = int(d[col])
            except TypeError:
                pass
        if d["json"] is not None:
            d["json"] = ast.literal_eval(d["json"])

    update_cols = ("coq_user_id", "pax_count", "fng_count", "timestamp", "ts_edited", "backblast", "json")

    beatdowns_insert_sql = insert_statement(cb, insert_values, update_cols)

    with engine.begin() as cnxn:
        cnxn.execute(beatdowns_insert_sql)

    dtypes = dict(
        beatdown_id=pd.StringDtype(),
        ao_id=pd.StringDtype(),
        q_user_id=pd.StringDtype(),
        coq_user_id=pd.StringDtype(),
        pax_count=pd.Int16Dtype(),
        fng_count=pd.Int16Dtype(),
        timestamp=pd.Float64Dtype(),
        ts_edited=pd.Float64Dtype(),
        backblast=pd.StringDtype(),
        json=pd.StringDtype(),
    )

    df_beatdowns = pd.read_sql(select(cb), engine, parse_dates="bd_date", dtype=dtypes)
    df_beatdowns.q_user_id = (
        df_beatdowns.q_user_id.astype(pd.Float64Dtype()).astype(pd.Int64Dtype()).astype(pd.StringDtype())
    )
    return df_beatdowns


def build_attendance(
    df_attendance: pd.DataFrame,
    df_users_dup: pd.DataFrame,
    df_aos: pd.DataFrame,
    df_beatdowns: pd.DataFrame,
    engine: Engine,
    metadata: MetaData,
) -> None:
    """
    Returns None. This process usees all the proir updates to users, AOs and beatdowns to update attendance records in the source
    tables.

    :param df_attendance: pandas DataFrame object containing each region's attendance information
    :type df_attendance: pandas.DataFrame object
    :param df_beatdowns: pandas DataFrame object containing each region's beatdown information
    :type df_beatdowns: pandas.DataFrame object
    :param df_users_dup: pandas DataFrame object containing each region's users information
    :type df_users_dup: pandas.DataFrame object
    :param df_aos: pandas DataFrame object containing each region's AO information
    :type df_aos: pandas.DataFrame object
    :param engine: SQLAlchemy connection engine to MySQL
    :type engine: sqlalchemy.engine.Engine object
    :param metadata: collection of reflected table metadata
    :type metadata: SQLAlchemy MetaData
    :rtype: None
    :return: None
    """

    logging.info("building attendance...")
    catt = metadata.tables["weaselbot.combined_attendance"]
    df_attendance["slack_user_id"] = df_attendance["slack_user_id"].apply(extract_user_id).astype(pd.StringDtype())
    df_attendance["slack_q_user_id"] = df_attendance["slack_q_user_id"].apply(extract_user_id).astype(pd.StringDtype())
    df_attendance = (
        (
            df_attendance.merge(
                df_users_dup[["slack_user_id", "user_id", "region_id"]],
                left_on=["slack_q_user_id", "region_id"],
                right_on=["slack_user_id", "region_id"],
                how="left",
            )
            .rename({"user_id": "q_user_id", "slack_user_id_x": "slack_user_id"}, axis=1)
            .drop("slack_user_id_y", axis=1)
        )
        .merge(
            df_users_dup[["slack_user_id", "user_id", "region_id"]],
            on=["slack_user_id", "region_id"],
            how="left",
        )
        .merge(
            df_aos[["slack_channel_id", "ao_id", "region_id"]],
            on=["slack_channel_id", "region_id"],
            how="left",
        )
        .merge(
            df_beatdowns[["beatdown_id", "bd_date", "q_user_id", "ao_id"]],
            on=["bd_date", "q_user_id", "ao_id"],
            how="left",
        )
    )

    df_attendance.drop_duplicates(subset=["beatdown_id", "user_id"], inplace=True)
    df_attendance = df_attendance[df_attendance["beatdown_id"].notnull()]
    df_attendance = df_attendance[df_attendance["user_id"].notnull()]

    insert_values = df_attendance[["beatdown_id", "user_id", "json"]].to_dict("records")

    for d in insert_values:
        for col in ("beatdown_id", "user_id"):
            try:
                d[col] = int(d[col])
            except TypeError:
                pass

    update_cols = ("beatdown_id", "json")
    attendance_insert_sql = insert_statement(catt, insert_values, update_cols)

    with engine.begin() as cnxn:
        cnxn.execute(attendance_insert_sql)


def build_regions(engine: Engine, metadata: MetaData) -> None:
    """Run the regions querie again after all updates are made in order to capture any changes.

    :param engine: SQLAlchemy connection engine to MySQL
    :type engine: sqlalchemy.engine.Engine object
    :param metadata: collection of reflected table metadata
    :type metadata: SQLAlchemy MetaData
    :rtype: None
    :return: None
    """

    cr = metadata.tables["weaselbot.combined_regions"]
    paxminer_region_sql = paxminer_region_query(metadata, cr)
    df_regions = pd.read_sql(paxminer_region_sql, engine)
    insert_values = df_regions[["schema_name", "region_name", "max_timestamp", "max_ts_edited"]].to_dict("records")
    update_cols = ("region_name", "max_timestamp", "max_ts_edited")
    region_insert_sql = insert_statement(cr, insert_values, update_cols)

    with engine.begin() as cnxn:
        cnxn.execute(region_insert_sql)


def main() -> None:
    """
    Main function call. This is the process flow for the original code. If not called from the
    command line, then follow this sequence of steps for proper implementation.
    """
    logging.basicConfig(format="%(asctime)s [%(levelname)s]:%(message)s",
                        level=logging.INFO,
                        datefmt="%Y-%m-%d %H:%M:%S")
    engine = mysql_connection()
    metadata = MetaData()

    metadata.reflect(engine, schema="weaselbot")
    Table("regions", metadata, autoload_with=engine, schema="paxminer")

    df_regions = region_queries(engine, metadata)

    df_users_dup_list, df_aos_list, df_beatdowns_list, df_attendance_list = [], [], [], []
    for row in df_regions.itertuples(index=False):
        df_users_dup_list.append(pull_users(row, engine, metadata))
        df_aos_list.append(pull_aos(row, engine, metadata))
        df_beatdowns_list.append(pull_beatdowns(row, engine, metadata))
        df_attendance_list.append(pull_attendance(row, engine, metadata))

    df_users_dup = pd.concat([x for x in df_users_dup_list if not x.empty])
    df_aos = pd.concat([x for x in df_aos_list if not x.empty])
    df_beatdowns = pd.concat([x for x in df_beatdowns_list if not x.empty])
    df_attendance = pd.concat([x for x in df_attendance_list if not x.empty])

    df_beatdowns.ts_edited = df_beatdowns.ts_edited.replace("NA", pd.NA).astype(pd.Float64Dtype())

    logging.info(f"beatdowns to process: {len(df_beatdowns)}")
    df_users_dup = build_users(df_users_dup, df_attendance, engine, metadata)
    df_aos = build_aos(df_aos, engine, metadata)
    df_beatdowns = build_beatdowns(df_beatdowns, df_users_dup, df_aos, engine, metadata)
    build_attendance(df_attendance, df_users_dup, df_aos, df_beatdowns, engine, metadata)

    engine.dispose()


if __name__ == "__main__":
    main()
