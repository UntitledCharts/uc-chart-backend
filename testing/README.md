# How to launch
1. Install `requests` and `colorama`
2. Create a directory `assets` with files `jacket.png`, `music.mp3`, `level.data`
3. Create and fill config.py
4. Run using `python3 -m testing`

# Sorta docs
### test.Body()
#### Parameters
- `params` (`Optional[ {param:str : value:str} ]`): query parameters (`url?param=value`);
- `data` (`Optional[ {any: any} ]`): request body;
- `form_data` (`Optional[ {str: any} ]`): request form;
- `files` (`Optional[ {field:str : (filename:str, file:IO, mime_type:str)} ]`);
- `format_path` (`Optional[ {key:str : val:str} ]`): replaces `"{key}"` in url with `"val"`;
- `use_private_auth` (`Optional[ bool ] = False`): uses `config.server.auth` for authorizing.

### test.After()
#### Parameters
- `after` (`Callable`): uses this function as a dependency. If some function `x` uses `y` as a dependency and `y` fails, `x` gets skipped
- `value` (`Optional[ key:str ]`): if some function `x` has a dependency `y` and that `y` returned something, the return value will be passed as a kwarg to `x`: `x([key]=ret_val[y])`
- `use_for_auth` (`Optional[ bool ] = False`): return value of the dependency will be used as an authorization token

### test.Test()
#### Methods
- `start() -> None`
- `check(route: Callable) -> bool`: checks if `route` (a function decorated with `@test.Test.route(...)`) has been executed succesfully

### test.Test.route()
#### Parameters
- `path` (`str`): ..path. Should have a leading slash
- `method` (`str`): HTTP method supported by `requests.request`
- `dependencies` (`Optional[ [dependency:test.After()] ]`)

#### Description
A decorator for generators.

1. **First yield**:
The generator should yield either `None` or a `test.Body()`. Generator gets `requests.Response` sent back

2. **Second yield** (optional):
The value gets treated as a return value

A generator should accept kwargs if `value` is specified in any dependency
##### Example:
```py
@test.route(..., ..., dependencies=[After(..., value="arg_name")], After(..., value="second_arg"))
def handler(arg_name, second_arg):
    response: requests.Response = yield
    yield "some return value"
```