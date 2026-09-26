/** Whether a tile error is a connection/auth failure, not a bad query.

Same message on every tile used to mean "source is down". That is true for
auth and network; it is false when tiles share a relation and the SQL is
wrong — Snowflake 000904, a missing column, a binder error. The banner that
says "try sqldash source test" is reserved for the first class.
*/
export function looksLikeSourceFailure(message) {
  const text = String(message).toLowerCase();
  if (
    /sql compilation|invalid identifier|000904|\b42000\b|binder error|catalog error|syntax error|does not exist|undefined column|unrecognized name|no such table|referenced column/.test(
      text,
    )
  ) {
    return false;
  }
  return /connection|connect(?:ion)? (?:refused|timed|failed)|could not connect|authentication|login failed|network_timeout|econnrefused|ssl |certificate|password authentication|not authorized|access denied|no active warehouse|failed to connect/.test(
    text,
  );
}
