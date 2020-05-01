from pydantic import BaseModel
from typing import List
"""
These are request and response models used by
FastAPI to monitor/enforce the data types of the
JSON requests and responses.
"""

class MovieRec(BaseModel):
    movie_id: str 
    score: float 
    title: str 
    year: int 
    genres: List[str]
    poster_url: str


class Movie(BaseModel):
    movie_id: str 
    title: str 
    year: int 
    genres: List[str]
    poster_url: str


class RecInput(BaseModel):
    user_id: int
    num_recs: int = 10
    good_threshold: int = 4
    bad_threshold: int = 3
    harshness: int = 1


class RecOutput(BaseModel):
    data: List[MovieRec]


class SimInput(BaseModel):
    movie_id: str
    num_movies: int = 10


class SimOutput(BaseModel):
    data: List[MovieRec]


class ListInput(BaseModel):
    user_id: int 
    name: str 


class GetListOutput(BaseModel):
    data: List[Movie]
