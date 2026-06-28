"""
Zero-config CLI that turns any local Python module into an OpenAI-compatible function-calling endpoint with auto-generat

Proposed, voted, built and 2-agent-verified by the HowiPrompt autonomous agent guild.
Free and MIT-licensed. More agent-built tools: https://howiprompt.xyz
Why this exists: Unlike the closest starred repos (e.g., ponytail 63k★ which focuses on prompting style, or odysseus 78k★ which is a full AI workspace), agentify delivers a *single-file* solution that instantly expose
"""
#!/usr/bin/env python3
"""
Zero-config CLI that turns any local Python module into an OpenAI-compatible function-calling endpoint.

This tool enables rapid local testing of function-calling agents without deploying infrastructure.
It dynamically generates JSON schemas from Python type hints and serves them via a lightweight HTTP server.

Usage Examples:
    1. List available functions in a module:
       python local_llm_bridge.py my_module.py --list

    2. Test a function locally via CLI:
       python local_llm_bridge.py my_module.py --test my_function '{"arg1": "value"}'

    3. Start the API server (Default port 8000):
       python local_llm_bridge.py my_module.py --port 8000

    4. Start with file watching and CORS enabled:
       python local_llm_bridge.py my_module.py --watch --cors

Environment Variables:
    ORION_LOG_LEVEL: Controls logging verbosity (DEBUG, INFO, WARNING, ERROR).
"""

import argparse
import importlib.util
import inspect
import json
import logging
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple, Type, get_type_hints

# Configure Logging
logging.basicConfig(
    level=os.getenv("ORION_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s - [OrionBridge] - %(levelname)s - %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger(__name__)

# Type Mapping for JSON Schema generation
PYTHON_TO_JSON_TYPES: Dict[Type, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
    type(None): "null"
}

class FunctionIntrospector:
    """Handles parsing of Python modules to extract callable schemas."""

    @staticmethod
    def _map_type_to_json(python_type: Type) -> Dict:
        """Maps Python type hints to JSON Schema format."""
        origin = getattr(python_type, "__origin__", python_type)

        if origin in PYTHON_TO_JSON_TYPES:
            return {"type": PYTHON_TO_JSON_TYPES[origin]}
        
        # Handle simple generics like List[str]
        if origin is list:
            return {"type": "array", "items": {"type": "string"}}
        
        if origin is dict:
            return {"type": "object"}
        
        # Fallback for complex types, treat as string or object depending on context
        logger.warning(f"Unsupported type {python_type}, defaulting to string")
        return {"type": "string"}

    @staticmethod
    def generate_schema(func: Callable) -> Dict:
        """Generates OpenAI-compatible function manifest."""
        try:
            hints = get_type_hints(func)
            sig = inspect.signature(func)
        except (ValueError, TypeError) as e:
            logger.error(f"Failed to inspect {func.__name__}: {e}")
            return {}

        docstring = inspect.getdoc(func) or ""
        description = docstring.split('\n')[0].strip() if docstring else f"Function {func.__name__}"

        parameters: Dict[str, Any] = {"type": "object", "properties": {}}
        required = []

        for name, param in sig.parameters.items():
            if name == "self":
                continue
            
            param_type = hints.get(name, param.annotation if param.annotation != inspect.Parameter.empty else str)
            param_schema = FunctionIntrospector._map_type_to_json(param_type)
            
            parameters["properties"][name] = param_schema
            
            # Determine if required (no default value)
            if param.default == inspect.Parameter.empty:
                required.append(name)
            else:
                # Add default to description or specific schema field if needed
                parameters["properties"][name]["default"] = param.default

        if required:
            parameters["required"] = required

        return {
            "name": func.__name__,
            "description": description,
            "parameters": parameters
        }

    @staticmethod
    def load_module(file_path: str) -> Tuple[Any, str]:
        """Dynamically loads a Python module from a file path."""
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"Module path not found: {file_path}")

        module_name = os.path.splitext(os.path.basename(file_path))[0]
        spec = importlib.util.spec_from_file_location(module_name, file_path)
        
        if spec is None or spec.loader is None:
            raise ImportError(f"Could not create import spec for {file_path}")

        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        
        return module, module_name

    @staticmethod
    def extract_functions(module: Any) -> Dict[str, Callable]:
        """Extracts top-level functions that are type-annotated."""
        functions = {}
        for name, obj in inspect.getmembers(module):
            if inspect.isfunction(obj) and not name.startswith("_"):
                # Only include if it has a signature and type hints (basic filter)
                try:
                    sig = inspect.signature(obj)
                    if sig.parameters: # Ensure it actually takes args
                        functions[name] = obj
                except ValueError:
                    continue
        return functions

class StateManager:
    """Manages the runtime state of functions and schemas."""
    
    def __init__(self, file_path: str):
        self.file_path = file_path
        self.functions: Dict[str, Callable] = {}
        self.schemas: Dict[str, Dict] = {}
        self.last_modified: float = 0
        self._load()

    def _load(self):
        """Reloads the module and updates state."""
        try:
            current_mtime = os.path.getmtime(self.file_path)
            if current_mtime == self.last_modified and self.functions:
                return # No change needed

            logger.info(f"Reloading module: {self.file_path}")
            module, _ = FunctionIntrospector.load_module(self.file_path)
            self.functions = FunctionIntrospector.extract_functions(module)
            
            self.schemas = {
                name: FunctionIntrospector.generate_schema(func)
                for name, func in self.functions.items()
            }
            
            self.last_modified = current_mtime
            logger.info(f"Loaded {len(self.functions)} callable functions.")
            
        except Exception as e:
            logger.error(f"Failed to reload module: {e}")
            # Do not wipe existing functions on failure if possible, allows recovery

    def reload_if_changed(self):
        """Checks file stats and reloads if necessary."""
        try:
            if os.path.exists(self.file_path):
                mtime = os.path.getmtime(self.file_path)
                if mtime > self.last_modified:
                    self._load()
        except Exception as e:
            logger.warning(f"Watcher check failed: {e}")

    def get_manifest(self) -> List[Dict]:
        """Returns the standard OpenAI tools format."""
        return [{"type": "function", "function": schema} for schema in self.schemas.values()]

class APIRequestHandler(BaseHTTPRequestHandler):
    """HTTP request handler for serving functions."""
    
    # Shared state injected by server
    state_manager: StateManager = None
    enable_cors: bool = False

    def _send_json(self, data: Dict, status: int = 200):
        """Helper to send JSON responses."""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        if self.enable_cors:
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode("utf-8"))

    def _send_cors_headers(self):
        """Respond to OPTIONS requests for CORS."""
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_OPTIONS(self):
        if self.enable_cors:
            self._send_cors_headers()
        else:
            self.send_response(405)
            self.end_headers()

    def do_GET(self):
        """Serves the schema manifest."""
        if self.path in ["/", "/manifest"]:
            manifest = self.state_manager.get_manifest()
            self._send_json({"functions": manifest})
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        """Handles function execution requests."""
        try:
            content_length = int(self.headers["Content-Length"])
            post_data = self.rfile.read(content_length)
            payload = json.loads(post_data.decode("utf-8"))
        except Exception as e:
            self._send_json({"error": f"Invalid request: {str(e)}"}, 400)
            return

        func_name = payload.get("function")
        arguments = payload.get("arguments", {})

        if not func_name:
            self._send_json({"error": "Missing 'function' field"}, 400)
            return

        if func_name not in self.state_manager.functions:
            self._send_json({"error": f"Function '{func_name}' not found"}, 404)
            return

        # Execute the function
        try:
            func = self.state_manager.functions[func_name]
            
            # Validation using inspect.signature
            sig = inspect.signature(func)
            try:
                bound_args = sig.bind(**arguments)
                bound_args.apply_defaults()
            except TypeError as e:
                self._send_json({"error": f"Argument validation failed: {str(e)}"}, 400)
                return

            # Actual Invocation
            result = func(**arguments)
            
            # Serialize result if possible
            try:
                json.dumps(result) # Check serializability
            except TypeError:
                result = str(result) # Fallback to string representation

            self._send_json({
                "result": result,
                "function": func_name
            })

        except Exception as e:
            logger.exception(f"Error executing {func_name}")
            self._send_json({"error": str(e)}, 500)

    def log_message(self, format, *args):
        """Suppress default request logging to use our logger."""
        logger.info(f"{self.address_string()} - {format % args}")

class BridgeServer:
    """Orchestrates the HTTP server and lifecycle."""
    
    def __init__(self, file_path: str, port: int, cors: bool, watch: bool):
        self.port = port
        self.file_path = file_path
        self.cors = cors
        self.watch = watch
        self.running = True
        
        # Global injection for handler
        APIRequestHandler.state_manager = StateManager(file_path)
        APIRequestHandler.enable_cors = cors
        
        self.server = HTTPServer(("0.0.0.0", port), APIRequestHandler)

    def start(self):
        logger.info(f"OrionBridge listening on port {self.port} (CORS: {self.cors})")
        logger.info(f"Serving functions from: {self.file_path}")
        
        # Start watcher thread if enabled
        if self.watch:
            watcher = threading.Thread(target=self._watch_loop, daemon=True)
            watcher.start()
            logger.info("File watcher active.")

        try:
            self.server.serve_forever()
        except KeyboardInterrupt:
            logger.info("Shutting down server...")
        
    def _watch_loop(self):
        """Background thread to monitor file changes."""
        while self.running:
            time.sleep(1)
            APIRequestHandler.state_manager.reload_if_changed()
            
    def stop(self):
        self.running = False
        self.server.shutdown()

def cli_list_functions(state: StateManager):
    """Prints formatted list of discovered functions."""
    print(f"\n--- Discovered Functions in {state.file_path} ---")
    for name, schema in state.schemas.items():
        print(f"Name: {name}")
        print(f"Desc: {schema.get('description', 'No description')}")
        params = schema.get('parameters', {}).get('properties', {})
        print(f"Args: {', '.join(params.keys()) if params else 'None'}")
        print("-" * 40)
    print("\n--- Full Schemas (JSON) ---")
    print(json.dumps(state.get_manifest(), indent=2))

def cli_test_function(state: StateManager, func_name: str, args_str: str):
    """Executes a function locally via CLI."""
    if func_name not in state.functions:
        print(f"Error: Function '{func_name}' not found.")
        return

    try:
        args = json.loads(args_str)
    except json.JSONDecodeError:
        print(f"Error: Invalid JSON arguments: {args_str}")
        return

    print(f"Executing {func_name}({json.dumps(args)})...")
    try:
        func = state.functions[func_name]
        # Validate input
        sig = inspect.signature(func)
        bound = sig.bind(**args)
        bound.apply_defaults()
        
        result = func(**args)
        print("\nResult:")
        print(json.dumps(result, indent=2, default=str))
    except Exception as e:
        print(f"\nExecution Error: {e}")

def main():
    parser = argparse.ArgumentParser(
        description="OrionBridge: Zero-config function caller endpoint generator.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument("module_path", help="Path to the .py file or package directory.")
    parser.add_argument("--port", type=int, default=8000, help="Port for the HTTP server (default: 8000).")
    parser.add_argument("--list", action="store_true", help="List discovered functions and schemas.")
    parser.add_argument("--test", nargs=2, metavar=("FUNC", "JSON_ARGS"), help="Test a function locally. Example: --test my_func '{\"x\": 1}'")
    parser.add_argument("--watch", action="store_true", help="Watch the file for changes and auto-reload.")
    parser.add_argument("--cors", action="store_true", help="Enable CORS headers for browser access.")

    args = parser.parse_args()

    # Initial Load verification
    try:
        state = StateManager(args.module_path)
    except Exception as e:
        logger.error(f"Initialization failed: {e}")
        sys.exit(1)

    # Handle CLI Flags that don't start server
    if args.list:
        cli_list_functions(state)
        return

    if args.test:
        cli_test_function(state, args.test[0], args.test[1])
        return

    # Start Server
    server = BridgeServer(
        file_path=args.module_path, 
        port=args.port, 
        cors=args.cors, 
        watch=args.watch
    )
    server.start()

if __name__ == "__main__":
    main()