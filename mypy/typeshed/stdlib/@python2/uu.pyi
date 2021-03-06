from typing import BinaryIO, Optional, Text, Union

_File = Union[Text, BinaryIO]

class Error(Exception): ...

def encode(in_file: _File, out_file: _File, name: Optional[str] = ..., mode: Optional[int] = ...) -> None: ...
def decode(in_file: _File, out_file: Optional[_File] = ..., mode: Optional[int] = ..., quiet: int = ...) -> None: ...
