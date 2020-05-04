import pandas as pd
import numpy as np
import gensim
import re
import os 
import psycopg2
import json
import hashlib
from datetime import datetime


class MovieUtility(object):
    """ Movie utility class that uses a W2V model to recommend movies
    based on the movies a user likes and dislikes """

    def __init__(self, model_path):
        """ Initialize model with name of .model file """
        self.model_path = model_path
        self.model = self.__load_model()
        self.connection = self.__get_connection()
        self.id_book = self.__get_id_book()
    
    # ------- Start Private Methods -------
    def __get_connection(self):
        return psycopg2.connect(
            database  =  os.getenv('DB_NAME'),
            user      =  os.getenv('DB_USER'),
            password  =  os.getenv('DB_PASSWORD'),
            host      =  os.getenv('HOST'),
            port      =  os.getenv('PORT')
        )
    
    def __get_cursor(self):
        """ Grabs cursor from self.connection """
        try:
            cursor = self.conneciton.cursor()
            return cursor 
        except:
            self.connection = self.__get_connection()
            return self.connection.cursor()

    def __get_id_book(self):
        """ Gets movie data from database to merge with recommendations """
        self.cursor_dog = self.__get_cursor()
        query = "SELECT movie_id, primary_title, start_year, genres, poster_url FROM movies;"
        self.cursor_dog.execute(query)
        movie_sql= self.cursor_dog.fetchall()
        id_book = pd.DataFrame(movie_sql, columns = ['movie_id', 'title', 'year', 'genres', 'poster_url'])
        self.cursor_dog.close()
        return id_book

    def __load_model(self):
        """ Get the model object for this instance, loading it if it's not already loaded """
        w2v_model = gensim.models.Word2Vec.load(self.model_path)
        # Keep only the normalized vectors.
        # This saves memory but makes the model untrainable (read-only).
        w2v_model.init_sims(replace=True)
        self.model = w2v_model
        return self.model

    def __get_info(self, recs):
        """ Merging recs with id_book to get movie info """
        return pd.merge(recs, self.id_book, how='left', on='movie_id')
    
    def __get_JSON(self, rec_df):
        """ 
        Turn predictions into JSON
        Callers:
            - get_recommendations
            - get_similar_movies
            - get_movie_list
        """
        names = rec_df.columns
        rec_json = []

        for i in range(rec_df.shape[0]):
            rec = dict(rec_df.iloc[i].to_dict())
            rec['score'] = float(rec['score']) if not isinstance(rec['score'], str) else 0.0
            rec_json.append({
                    'movie_id': rec['movie_id'], 
                    'score': rec['score'], 
                    'title': rec['title'],
                    'year': int(rec['year']),
                    'genres': rec['genres'].split(','), 
                    'poster_url': rec['poster_url']
                    })

        return rec_json
    
    def __prep_data(self, ratings_df, watched_df=None, watchlist_df=None, good_threshold=4, bad_threshold=3):
        """Converts dataframes of exported Letterboxd data to lists of movie_ids.

        Parameters
        ----------
        ratings_df : pd dataframe
            user ratings.

        watched_df : pd dataframe
            user watch history.

        watchlist_df : pd dataframe
            list of movies the user wants to watch.
            Used in val_list for scoring the model's performance.

        good_threshold : int
            Minimum star rating (5pt scale) for a movie to be considered "enjoyed" by the user.

        bad_threshold : int
            Maximum star rating (5pt scale) for a movie to be considered "disliked" by the user.


        Returns
        -------
        tuple of lists of ids.
            (good_list, bad_list, hist_list, val_list, ratings_dict)
            point scale seems to be 5
            good_list is ratings greater than good_threashold
            bad_list is ratings below bad_threshold
            neutral_list is movies not in good or bad list
            val_list is just movies in watched_df
            hist_list seems to be movies not in good or bad too?
            ratings_dict all ids mapped to their ratings
        """
        try:
            # split according to user rating
            good_df = ratings_df[ratings_df['rating'] >= good_threshold]
            bad_df = ratings_df[ratings_df['rating'] <= bad_threshold]
            neutral_df = ratings_df[(ratings_df['rating'] > bad_threshold) & (ratings_df['rating'] < good_threshold)]
            
            # convert dataframes to lists
            good_list = good_df['movie_id'].to_list()
            bad_list = bad_df['movie_id'].to_list()
            neutral_list = neutral_df['movie_id'].to_list()

        except Exception as e:
            print("Error making good, bad and neutral list")
            raise Exception(e)

        ratings_dict = pd.Series(ratings_df['rating'].values,index=ratings_df['movie_id']).to_dict()

        if watched_df is not None:
            # Construct list of watched movies that aren't rated "good" or "bad"
            hist_list = ratings_df[~ratings_df['movie_id'].isin(good_list+bad_list)]['movie_id'].to_list()
        else: hist_list = neutral_list

        if watchlist_df is not None:
            # gets list of movies user wants to watch for validation
            val_list = watchlist_df['movie_id'].tolist()
        else: val_list = []

        return (good_list, bad_list, hist_list, val_list, ratings_dict)

    def __predict(self, input, bad_movies=[], hist_list=[], val_list=[],
                ratings_dict = {}, checked_list=[], rejected_list=[],
                n=50, harshness=1):
        """Returns a list of recommendations, given a list of movies.

        Parameters
        ----------

            input : iterable
                List of movies that the user likes.

            bad_movies : iterable
                List of movies that the user dislikes.

            hist_list : iterable
                List of movies the user has seen.

            val_list : iterable
                List of movies the user has already indicated interest in.

            ratings_dict : dictionary
                Dictionary of movie_id keys, user rating values.

            checked_list : iterable
                List of movies the user likes on the feedback form.

            rejected_list : iterable
                List of movies the user dislikes on the feedback form.

            n : int
                Number of recommendations to return.

            harshness : int
                Weighting to apply to disliked movies.
                Ex:
                    1 - most strongly account for disliked movies.
                    3 - divide "disliked movies" vector by 3.

        Returns
        -------
        A list of tuples
            (Movie ID, Similarity score)
        """

        clf = self.model
        # list for storing duplicates for scoring
        dupes = []

        def _aggregate_vectors(movies, feedback_list=[]):
            """ Gets the vector average of a list of movies """
            movie_vec = []
            for i in movies:
                try:
                    m_vec = clf[i]  # get the vector for each movie
                    if ratings_dict:
                        try:
                            r = ratings_dict[i] # get user_rating for each movie
                            # Use a polynomial to weight the movie by rating.
                            # This equation is somewhat arbitrary. I just fit a polynomial
                            # to some weights that look good. The effect is to raise
                            # the importance of 1, 2, 9, and 10 star ratings to about 1.8.
                            w = ((r**3)*-0.00143) + ((r**2)*0.0533) + (r*-0.4695) + 2.1867
                            m_vec = m_vec * w
                        except KeyError:
                            continue
                    movie_vec.append(m_vec)
                except KeyError:
                    continue
            if feedback_list:
                for i in feedback_list:
                    try:
                        f_vec = clf[i]
                        movie_vec.append(f_vec*1.8) # weight feedback by changing multiplier here
                    except KeyError:
                        continue
            return np.mean(movie_vec, axis=0)

        def _similar_movies(v, n, bad_movies=[]):
            """ Aggregates movies and finds n vectors with highest cosine similarity """
            if bad_movies:
                v = _remove_dislikes(bad_movies, v, harshness=harshness)
            return clf.similar_by_vector(v, topn=n+1)[1:]

        def _remove_dupes(recs, input, bad_movies, hist_list=[], feedback_list=[]):
            """ Remove any recommended IDs that were in the input list """
            all_rated = input + bad_movies + hist_list + feedback_list
            nonlocal dupes
            dupes = [x for x in recs if x[0] in input]
            return [x for x in recs if x[0] not in all_rated]

        def _remove_dislikes(bad_movies, good_movies_vec, rejected_list=[], harshness=1):
            """ Takes a list of movies that the user dislikes.
            Their embeddings are averaged,
            and subtracted from the input. """
            bad_vec = _aggregate_vectors(bad_movies, rejected_list)
            bad_vec = bad_vec / harshness
            return good_movies_vec - bad_vec

        aggregated = _aggregate_vectors(input, checked_list)
        recs = _similar_movies(aggregated, n, bad_movies)
        recs = _remove_dupes(recs, input, bad_movies, hist_list, checked_list + rejected_list)
        return recs

    def __get_list_preview(self, data):
        """ 
        Turns list preview sql into an object
        Callers:
            - get_user_lists
            - get_all_lists 
        """
        return {
            "list_id": data[0], 
            "name": data[1],
            "private": data[2]
        }
    
    def __run_query(self, query, params, commit=False, fetch="one"):
        self.cursor_dog = self.__get_cursor()
        if params is None:
            self.cursor_dog.execute(query)
        else:
            self.cursor_dog.execute(query, params)
        result = None
        if fetch == "one":
            result = self.cursor_dog.fetchone()[0]
        elif fetch == "all": 
            result = self.cursor_dog.fetchall()
        if commit:
            self.connection.commit()
        self.cursor_dog.close()
        return result
    # ------- End Private Methods -------

    # ------- Start Public Methods -------
    def create_movie_list(self, payload):
        """ Creates a MovieList """
        query = """INSERT INTO movie_lists
        (user_id, name, private) VALUES (%s, %s, %s) RETURNING list_id;"""
        list_id = self.__run_query(
            query, 
            (payload.user_id, payload.name, payload.private),
            commit=True)
        return {
            "list_id": list_id,
            "name": payload.name,
            "private": payload.private 
        }
    
    def get_movie_list(self, list_id):
        """ Gets all movies in MovieList and the associated recs """
        query = """SELECT l.movie_id, m.primary_title, m.start_year, m.genres, m.poster_url 
        FROM list_movies AS l LEFT JOIN movies AS m ON l.movie_id = m.movie_id
        WHERE l.list_id = %s;"""
        list_sql = self.__run_query(
            query, 
            (list_id,),
            fetch="all")
        list_json = {
            "data": [],
            "recs": []
        }
        if len(list_sql) > 0:
            movie_ids = []
            for movie in list_sql:
                movie_ids.append(movie[0])
                list_json["data"].append({
                    "movie_id": movie[0],
                    "title": movie[1],
                    "year": movie[2],
                    "genres": movie[3].split(","),
                    "poster_url": movie[4]
                })
            w2v_preds = self.__predict(movie_ids)
            df_w2v = pd.DataFrame(w2v_preds, columns=['movie_id', 'score'])
            # get movie info using movie_id
            rec_data = self.__get_info(df_w2v)
            rec_data = rec_data.fillna("None")
            rec_json = self.__get_JSON(rec_data)
            list_json["recs"] = rec_json
        return list_json
    
    def get_user_lists(self, user_id):
        """ Get user's MovieLists """
        query = "SELECT list_id, name, private FROM movie_lists WHERE user_id = %s;"
        user_lists = self.__run_query(
            query, 
            (user_id,),
            fetch="all")
        user_lists_json = [self.__get_list_preview(elem) for elem in user_lists]
        return user_lists_json
    
    def get_all_lists(self):
        """ Get all MovieLists """
        query = "SELECT list_id, name, private FROM movie_lists WHERE private=FALSE;"
        lists = self.__run_query(
            query, 
            None,
            fetch="all")
        lists_json = [self.__get_list_preview(elem) for elem in lists]
        return lists_json
    
    def add_to_movie_list(self, list_id, movie_id):
        """ Add movie to a MovieList """
        query = """INSERT INTO list_movies
        (list_id, movie_id) VALUES (%s, %s);"""
        self.__run_query(
            query, 
            (list_id, movie_id),
            commit=True,
            fetch="none")
        return "Success"
    
    def remove_from_movie_list(self, list_id, movie_id):
        """ Remove movie from a MovieList """
        query = "DELETE FROM list_movies WHERE list_id = %s AND movie_id = %s;"
        self.__run_query(
            query, 
            (list_id, movie_id),
            commit=True,
            fetch="none")
        return "Success"
    

    def delete_movie_list(self, list_id):
        """ Delete a MovieList """
        query = "DELETE FROM movie_lists WHERE list_id = %s RETURNING user_id, private;"
        result = self.__run_query(
            query, 
            (list_id,),
            commit=True,
            fetch="all")[0]
        return result

    def get_most_similar_title(self, id, id_list):
        """ Get the title of the most similar movie to id from id_list """
        clf = self.model
        vocab = clf.wv.vocab
        if id not in vocab:
            return ""
        id_list = [id for id in id_list if id in vocab] # ensure all in vocab
        id_book = self.id_book
        match = clf.wv.most_similar_to_given(id, id_list)
        return match
    
    def get_service_providers(self, movie_id):
        """ Get the service providers of a given movie_id """
        self.cursor_dog = self.__get_cursor()
        query = """
        SELECT m.provider_id, p.name, p.logo_url, m.provider_movie_url, 
        m.presentation_type, m.monetization_type
        FROM movie_providers AS m
        LEFT JOIN providers AS p ON m .provider_id = p.provider_id
        WHERE m.movie_id = %s; 
        """
        self.cursor_dog.execute(query, (movie_id,))
        prov_sql = self.cursor_dog.fetchall()
        prov_json = {
            "data": []
        }
        for provider in prov_sql:
            prov_json["data"].append({
                "provider_id": provider[0],
                "name": provider[1],
                "logo": str(provider[2]),
                "link": provider[3],
                "presentation_type": provider[4],
                "monetization_type": provider[5]
            })
        return prov_json
    
    def get_similar_movies(self, payload):
        """ Gets movies with highest cosine similarity """
        # request data
        movie_id = payload.movie_id 
        n = payload.num_movies
        # get model
        clf = self.model
        # could check if id is in vocab
        m_vec = clf[movie_id]
        movies_df = pd.DataFrame(clf.similar_by_vector(m_vec, topn=n+1)[1:], columns=['movie_id', 'score'])
        result_df = self.__get_info(movies_df)
        return {
            "data": self.__get_JSON(result_df)
            }
    
    def get_recommendations(self, payload, background_tasker):
        """ Uses user's ratings to generate recommendations """
        # request data 
        user_id = payload.user_id
        n = payload.num_recs
        good_threshold = payload.good_threshold
        bad_threshold = payload.bad_threshold
        harshness = payload.harshness

        # create cursor
        self.cursor_dog = self.__get_cursor()

        # Check if user has ratings data 
        query = "SELECT date, movie_id, rating FROM user_ratings WHERE user_id=%s;"
        self.cursor_dog.execute(query, (user_id,))
        ratings_sql= self.cursor_dog.fetchall()
        ratings = pd.DataFrame(ratings_sql, columns=['date', 'movie_id', 'rating'])
        if ratings.shape[0] == 0:
            self.cursor_dog.close()
            return "User does not have ratings"

        # Get user watchlist, willnotwatchlist, watched
        query = "SELECT date, movie_id FROM user_watchlist WHERE user_id=%s;"
        self.cursor_dog.execute(query, (user_id,))
        watchlist_sql= self.cursor_dog.fetchall()
        watchlist = pd.DataFrame(watchlist_sql, columns=['date', 'movie_id'])

        query = "SELECT date, movie_id FROM user_watched WHERE user_id=%s;"
        self.cursor_dog.execute(query, (user_id,))
        watched_sql= self.cursor_dog.fetchall()
        watched = pd.DataFrame(watched_sql, columns=['date', 'movie_id'])

        query = "SELECT date, movie_id FROM user_willnotwatchlist WHERE user_id=%s;"
        self.cursor_dog.execute(query, (user_id,))
        willnotwatch_sql= self.cursor_dog.fetchall()
        willnotwatchlist_df = pd.DataFrame(willnotwatch_sql, columns = ['date', 'movie_id'])

        # Prepare data
        good_list, bad_list, hist_list, val_list, ratings_dict = self.__prep_data(
            ratings, watched, watchlist, good_threshold=good_threshold, bad_threshold=bad_threshold
            )

        # Run prediction with parameters then wrangle output
        w2v_preds = self.__predict(good_list, bad_list, hist_list, val_list, ratings_dict, harshness=harshness, n=n)
        df_w2v = pd.DataFrame(w2v_preds, columns=['movie_id', 'score'])

        # get movie info using movie_id
        rec_data = self.__get_info(df_w2v)
        rec_data = rec_data.fillna("None")

        def _commit_to_database(model_recs, user_id, num_recs, good, bad, harsh): 
            """ Commit recommendations to the database """
            date = datetime.now()
            model_type = "ratings"

            create_rec = """
            INSERT INTO recommendations 
            (user_id, date, model_type) 
            VALUES (%s, %s, %s) RETURNING recommendation_id;
            """
            self.cursor_dog.execute(create_rec, (user_id, date, model_type))
            rec_id = self.cursor_dog.fetchone()[0]

            create_movie_rec = """
            INSERT INTO recommendations_movies
            (recommendation_id, movie_number, movie_id, num_recs, good_threshold, bad_threshold, harshness)
            VALUES (%s, %s, %s, %s, %s, %s, %s);
            """

            for num, movie in enumerate(model_recs):
                self.cursor_dog.execute(
                    create_movie_rec, 
                    (rec_id, num+1, movie['movie_id'], num_recs, good, bad, harsh))

            self.connection.commit()
            self.cursor_dog.close()

        rec_json = self.__get_JSON(rec_data)

        # add background task to commit recs to DB
        background_tasker.add_task(
            _commit_to_database, 
            rec_json, user_id, n, good_threshold, bad_threshold, harshness)

        return {
                "data": rec_json
            }
    # ------- End Public Methods -------