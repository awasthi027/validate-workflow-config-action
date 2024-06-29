# validate_workflow_config.py
import sys
import json
from jsonschema import validate
from jsonschema.exceptions import ValidationError, SchemaError
import ast
import re

# Flag to toggle debugging output.
# Leave False in production else it will taint the json returned from this script.
DEBUG=False

def remove_disabled_elements(data):
    if isinstance(data, dict):
        if data.get('disabled') == True:
            return None
        else:
            cleaned_dict = {key: remove_disabled_elements(value) for key, value in data.items() if remove_disabled_elements(value) is not None}
            # If all elements are removed, resulting in an empty dict, return None
            return None if not cleaned_dict else cleaned_dict
    elif isinstance(data, list):
        cleaned_list = [remove_disabled_elements(item) for item in data if remove_disabled_elements(item) is not None]
        # If the list becomes empty after removing disabled elements, return None to indicate removal of the parent array
        return None if not cleaned_list else cleaned_list
    else:
        return data

def validate_and_output_json(file_path, schema_paths=[]):
    try:
        data = None
        with open(file_path, 'r') as file:
            data = json.load(file)  # Load and parse the JSON file

        # Apply runtime overrides before validating as these variables could cause a failure in schema validation
        cleaned_data = apply_runtime_overrides(data, runtime_overrides)

        for schema_path in schema_paths:
            with open(schema_path, 'r') as schema_file:
                schema = json.load(schema_file)  # Load the schema
                validate(instance=cleaned_data, schema=schema)  # Validate the loaded data against the schema

        # Remove elements with "disabled": true
        cleaned_data = remove_disabled_elements(cleaned_data)

        # Convert JSON to a single-line string
        if DEBUG:
            return json.dumps(cleaned_data, indent=2)
        else:
            return json.dumps(cleaned_data)
    except FileNotFoundError:
        print(f"File not found: {file_path}")
        sys.exit(f"File not found: {file_path}")  # Exit with error if file not found
    except json.JSONDecodeError as e:
        print(f"Invalid JSON content in {file_path}\nError: {e}", file=sys.stderr)
        sys.exit(f"Invalid JSON content in {file_path}\nError: {e}")  # Exit with error if JSON is invalid
    except ValidationError as e:
        print(f"JSON validation error\nError: {e}", file=sys.stderr)
        sys.exit(f"JSON validation error\nError: {e}")
    except SchemaError as e:
        print(f"JSON Schema error\nError: {e}", file=sys.stderr)
        sys.exit(f"JSON Schema error\nError: {e}")

def apply_runtime_overrides(data, runtime_overrides):
    marker = "___TEMP_MARKER___"

    # Extract the "defaults" section from the JSON data
    defaults = data.get("defaults", [])

    # If there's nothing to override then bail out early
    if not defaults and not runtime_overrides:
        if DEBUG:
            print("No defaults and no runtime overrides. Returning untouched json")
        return data

    # Remove the "defaults" section from the JSON data
    if defaults:
        if DEBUG:
            print("Removing defaults from json")
        data.pop("defaults", None)

    # Parse the runtime overrides into a dictionary
    overrides = {}
    if runtime_overrides:
        for pair in runtime_overrides.split(";"):
            key, value = pair.split("=", 1)
            if value.startswith("{") and value.endswith("}"):
                # Parse the value as an object if it is a string representation of an object
                overrides[key] = json.loads(value)
            elif value.startswith("[") and value.endswith("]"):
                # Parse the value as a list if it is a string representation of a list
                overrides[key] = ast.literal_eval(value)
            elif value.lower() == "true":
                overrides[key] = True
            elif value.lower() == "false":
                overrides[key] = False
            elif value.lower() == "null" or value.lower() == "none":
                overrides[key] = None
            else:
                try:
                    # Try parsing the value as an integer
                    overrides[key] = int(value)
                except ValueError:
                    try:
                        # Try parsing the value as a float
                        overrides[key] = float(value)
                    except ValueError:
                        # If parsing as an integer or float fails, treat it as a string
                        overrides[key] = value.strip('"')

    # Merge the defaults with the runtime overrides
    for default in defaults:
        for key, value in default.items():
            if key not in overrides:
                overrides[key] = value

    if DEBUG:
        print("Overrides:", overrides)

    def safe_evaluate_condition(expression, context):
        """
        Safely evaluate a Python expression using an AST parsed tree to only allow
        certain types of operations.
        """
        try:
            tree = ast.parse(expression, mode='eval')

            # Allowed node types (limit to those necessary for simple if/else)
            allowed_types = (ast.Expression, ast.IfExp, ast.Compare, ast.BoolOp, ast.Name, ast.Load, ast.Constant, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE)

            for node in ast.walk(tree):
                if not isinstance(node, allowed_types):
                    raise ValueError("Unsupported operation: {}".format(type(node).__name__))

            # Compile and evaluate the safe tree
            code = compile(tree, '<string>', 'eval')
            return eval(code, {'__builtins__': None}, context)
        except TypeError as e:
            if "'NoneType' object is not subscriptable" in str(e):
                # Variable is not defined, exit the script immediately (same as schema failure)
                print("Variable not defined, failing build")
                sys.exit(f"RVS conditional error\nError: {expression}")
            else:
                print(f"Error evaluating conditional: {e}")
                raise
        except Exception as e:
            print(f"Error evaluating expression safely: {e}")
            raise

    def evaluate_conditional(expression):
        """Evaluates a Python conditional expression from a string.
    
        Args:
            expression (str): The Python conditional expression to evaluate.
            
        Returns:
            The result of evaluating the expression.
        """
        # Create a dictionary to hold the variables used in the condition
        locals_dict = {}

        # filter overrides to exclude dictionaries or lists for conditionals
        for key, value in overrides.items():
            if isinstance(value, (dict, list)):
                continue  # Skip replacing complex objects
            locals_dict[key] = value

        try:
            if DEBUG:
                print(f"evaluating condition: {expression}")
            result = safe_evaluate_condition(expression, locals_dict)
            return result
        except Exception as e:
            print(f"Error evaluating conditional: {e}")
            raise

    # Perform string replacement for variables in the JSON data
    def replace_variables(obj, key_path=""):
        if DEBUG:
            print(f"replace_variables called with obj: {obj}, key_path: {key_path}")
        if isinstance(obj, dict):
            for key, value in obj.items():
                current_key_path = f"{key_path}.{key}" if key_path else key
                if DEBUG:
                    print(f"Processing dict item: {key} = {value}, current_key_path: {current_key_path}")
                if isinstance(value, str):
                    if value.startswith(marker):
                        variable = value.replace(marker, "")
                        replacement = overrides.get(variable)
                        if DEBUG:
                            print(f"Found marker in string: {value}, variable: {variable}, replacement: {replacement}")
                        obj[key] = replacement
                        if DEBUG:
                            print(f"Replaced marker with: {obj[key]}")
                    else:
                        obj[key] = replace_variable(value, current_key_path)
                        if DEBUG:
                            print(f"Replaced variable in string: {key} = {obj[key]}")
                        if isinstance(obj[key], str) and obj[key].startswith(marker):
                            variable = obj[key].replace(marker, "")
                            replacement = overrides.get(variable)
                            if DEBUG:
                                print(f"Found marker after replacement: {obj[key]}, variable: {variable}, replacement: {replacement}")
                            obj[key] = replacement
                            if DEBUG:
                                print(f"Replaced marker with: {obj[key]}")
                else:
                    replace_variables(value, current_key_path)
        elif isinstance(obj, list):
            for i, item in enumerate(obj):
                current_key_path = f"{key_path}[{i}]"
                if DEBUG:
                    print(f"Processing list item: {item}, current_key_path: {current_key_path}")
                if isinstance(item, str):
                    if item.startswith(marker):
                        variable = item.replace(marker, "")
                        replacement = overrides.get(variable)
                        if DEBUG:
                            print(f"Found marker in list item: {item}, variable: {variable}, replacement: {replacement}")
                        obj[i] = replacement
                        if DEBUG:
                            print(f"Replaced marker in list with: {obj[i]}")
                    else:
                        obj[i] = replace_variable(item, current_key_path)
                        if DEBUG:
                            print(f"Replaced variable in list item: {i} = {obj[i]}")
                        if isinstance(obj[i], str) and obj[i].startswith(marker):
                            variable = obj[i].replace(marker, "")
                            replacement = overrides.get(variable)
                            if DEBUG:
                                print(f"Found marker after replacement: {obj[i]}, variable: {variable}, replacement: {replacement}")
                            obj[i] = replacement
                            if DEBUG:
                                print(f"Replaced marker with: {obj[i]}")
                else:
                    replace_variables(item, current_key_path)
        return obj

    def replace_variable(value, key_path):
        if DEBUG:
            print(f"replace_variable called with value: {value}, key_path: {key_path}")
        if isinstance(value, str):
            if DEBUG:
                print(f"Replacing in: {key_path}")
            def replace(match):
                full_expression = match.group(1)  # Capture the entire expression
                if DEBUG:
                    print(f"full expression: {full_expression}")

                # Check if it's a conditional (starts with "if")
                if "if" in full_expression and "else" in full_expression:
                    try:
                        result = evaluate_conditional(full_expression)
                        if result is None:
                            return match.group(0)  # Return the original matched text if evaluation fails
                        else:
                            return str(result)
                    except ValueError as e:
                        print(f"Error parsing conditional: {e}")
                        return match.group(0)  # Return the original matched text if parsing fails
                # Otherwise, it's a regular variable substitution
                else:
                    replacement = overrides.get(full_expression)
                    if isinstance(replacement, (dict, list)):
                        return marker + full_expression
                    elif replacement is None:
                        return f"${{{full_expression}}}"
                    elif isinstance(replacement, bool):
                        return "true" if replacement else "false"
                    elif isinstance(replacement, (int, float)):
                        return str(replacement)
                    else:
                        # re.sub() requires a string so we have to cast the result to a string
                        return f'"{replacement}"' if isinstance(replacement, str) else str(replacement)
            result = re.sub(r"\${([^}]+)}", replace, value)
            if DEBUG:
                print(f"Result after regex substitution: {result}")
            if result.startswith('"') and result.endswith('"'):
                if DEBUG:
                    print("Removing double quotes")
                tmp=result[1:-1]
                if DEBUG:
                    print(f"Result after removing double quotes: {tmp}")
                return tmp
            elif result in ["true", "false"]:
                return result == "true"
            elif result in ["null"]:
                return result
            else:
                try:
                    return int(result)
                except ValueError:
                    try:
                        return float(result)
                    except ValueError:
                        return result
        elif isinstance(value, dict):
            return {k: replace_variable(v, f"{key_path}.{k}") for k, v in value.items()}
        elif isinstance(value, list):
            return [replace_variable(item, f"{key_path}[{i}]") for i, item in enumerate(value)]
        else:
            return value

    replace_variables(data)

    return data

if __name__ == "__main__":
    file_path = sys.argv[1]
    runtime_overrides = sys.argv[2]
    schema_paths = sys.argv[3:]
    content = validate_and_output_json(file_path, schema_paths)
    print(content)
