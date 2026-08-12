import json
import logging
import os
import uuid
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamoDB = boto3.resource("dynamodb")
table = dynamoDB.Table(os.environ["TODOS_TABLE"])

RESPONSE_HEADERS = {
    "Content-Type": "application/json",
}


class DecimalEncoder(json.JSONEncoder):
    """DynamoDB returns every number as Decimal, which json.dumps cannot handle."""

    def default(self, o):
        if isinstance(o, Decimal):
            return int(o) if o % 1 == 0 else float(o)
        return super().default(o)


def _response(status_code, payload):
    return {
        "statusCode": status_code,
        "headers": RESPONSE_HEADERS,
        "body": json.dumps(payload, cls=DecimalEncoder),
    }


def _error(status_code, message):
    return _response(status_code, {"message": message})


def _parse_body(event):
    """Return the request body as a dict, or raise ValueError with a readable reason."""
    raw = event.get("body")
    if not raw:
        raise ValueError("Request body is required")
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        raise ValueError("Request body must be valid JSON")
    if not isinstance(body, dict):
        raise ValueError("Request body must be a JSON object")
    return body


def _user_id(event):
    """Read the caller's Cognito subject out of the JWT claims.

    API Gateway has already validated the signature and expiry before we run,
    so the claims can be trusted here. 'sub' is the immutable user id -- the
    email can be changed later, so it must not be used as the partition key.
    """
    claims = (
        event.get("requestContext", {})
        .get("authorizer", {})
        .get("jwt", {})
        .get("claims", {})
    )
    user_id = claims.get("sub")
    if not user_id:
        raise PermissionError("No user identity in the request")
    return user_id


def _path_todo_id(event):
    todo_id = (event.get("pathParameters") or {}).get("todoId")
    if not todo_id:
        raise ValueError("todoId is required in the path")
    return todo_id


def _clean_title(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("'title' must be a non-empty string")
    return value.strip()


def create_todo(event, context):
    try:
        user_id = _user_id(event)
        body = _parse_body(event)
        title = _clean_title(body.get("title"))
    except PermissionError as exc:
        return _error(401, str(exc))
    except ValueError as exc:
        return _error(400, str(exc))

    todo_item = {
        "userId": user_id,
        "todoId": str(uuid.uuid4()),
        "title": title,
        "completed": False,
    }

    try:
        table.put_item(Item=todo_item)
    except ClientError:
        logger.exception("put_item failed")
        return _error(500, "Could not create todo")

    return _response(201, todo_item)


def list_todos(event, context):
    try:
        user_id = _user_id(event)
    except PermissionError as exc:
        return _error(401, str(exc))

    items = []
    query_args = {"KeyConditionExpression": Key("userId").eq(user_id)}

    try:
        # A single Query returns at most 1 MB, so follow LastEvaluatedKey until exhausted.
        while True:
            response = table.query(**query_args)
            items.extend(response.get("Items", []))
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                break
            query_args["ExclusiveStartKey"] = last_key
    except ClientError:
        logger.exception("query failed")
        return _error(500, "Could not list todos")

    return _response(200, {"count": len(items), "items": items})


def get_todo(event, context):
    try:
        user_id = _user_id(event)
        todo_id = _path_todo_id(event)
    except PermissionError as exc:
        return _error(401, str(exc))
    except ValueError as exc:
        return _error(400, str(exc))

    try:
        response = table.get_item(Key={"userId": user_id, "todoId": todo_id})
    except ClientError:
        logger.exception("get_item failed for todoId=%s", todo_id)
        return _error(500, "Could not get todo")

    item = response.get("Item")
    if not item:
        return _error(404, "Todo not found")

    return _response(200, item)


def update_todo(event, context):
    try:
        user_id = _user_id(event)
        todo_id = _path_todo_id(event)
        body = _parse_body(event)
    except PermissionError as exc:
        return _error(401, str(exc))
    except ValueError as exc:
        return _error(400, str(exc))

    updates = {}
    try:
        if "title" in body:
            updates["title"] = _clean_title(body["title"])
        if "completed" in body:
            if not isinstance(body["completed"], bool):
                raise ValueError("'completed' must be true or false")
            updates["completed"] = body["completed"]
    except ValueError as exc:
        return _error(400, str(exc))

    if not updates:
        return _error(400, "Provide at least one of 'title' or 'completed'")

    # Build the expression from whatever was supplied so untouched fields stay as they are.
    try:
        response = table.update_item(
            Key={"userId": user_id, "todoId": todo_id},
            UpdateExpression="SET " + ", ".join(f"#{name} = :{name}" for name in updates),
            ExpressionAttributeNames={f"#{name}": name for name in updates},
            ExpressionAttributeValues={f":{name}": value for name, value in updates.items()},
            ConditionExpression="attribute_exists(todoId)",
            ReturnValues="ALL_NEW",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return _error(404, "Todo not found")
        logger.exception("update_item failed for todoId=%s", todo_id)
        return _error(500, "Could not update todo")

    return _response(200, response["Attributes"])


def delete_todo(event, context):
    try:
        user_id = _user_id(event)
        todo_id = _path_todo_id(event)
    except PermissionError as exc:
        return _error(401, str(exc))
    except ValueError as exc:
        return _error(400, str(exc))

    try:
        table.delete_item(
            Key={"userId": user_id, "todoId": todo_id},
            ConditionExpression="attribute_exists(todoId)",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return _error(404, "Todo not found")
        logger.exception("delete_item failed for todoId=%s", todo_id)
        return _error(500, "Could not delete todo")

    return _response(200, {"message": "Todo deleted", "todoId": todo_id})


def hello(event, context):
    return {
        "statusCode": 200,
        "body": "Hello from Lambda!"
    }


def gabel(event, context):
    msg = "gabel is comminggggg."

    return {
        "statusCode": 201,
        "body": msg
    }
