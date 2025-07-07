from fastapi import FastAPI
from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime
import time
from newspaper import Article
import requests
import concurrent.futures

# ==== Khai báo FastAPI app ====
app = FastAPI()

# ==== API Keys ====
NEWS_API = '898c89c237e243599b404ebd48f2b6a3'
GNEWS_API = '3dd8a59498ec187ddeae7e78d573d108'


# ==== Pydantic model ====
class CrawlRequest(BaseModel):
    queries: List[str]
    lang: Optional[str] = "en"
    max: Optional[int] = 10
    from_date: Optional[str] = None
    to_date: Optional[str] = None
    content_type: Optional[str] = "text"  # hoặc "html"
    country: Optional[str] = "us"         # thêm country
    domains: Optional[str] = None         # thêm domain (cho NewsAPI)
    exclude_domains: Optional[str] = None # thêm exclude_domains (cho NewsAPI)



# ==== Adapter interface ====
from abc import ABC, abstractmethod

class NewsAPIAdapter(ABC):
    @abstractmethod
    def get_articles(self, query, **kwargs):
        pass

    @abstractmethod
    def format_response(self, response):
        pass


# ==== GNews ====
class GNewsAdapter(NewsAPIAdapter):
    def __init__(self, api_key):
        self.api_key = api_key
        self.base_url = "https://gnews.io/api/v4/search"

    def get_articles(self, query, **kwargs):
        params = {
            'q': query,
            'token': self.api_key,
            'lang': kwargs.get('lang', 'en'),
            'country': kwargs.get('country', 'us'),
            'max': kwargs.get('max', 10)
        }
        try:
            response = requests.get(self.base_url, params=params, timeout=10)
            response.raise_for_status()
            return self.format_response(response.json())
        except requests.RequestException as e:
            print(f"[GNews ERROR]: {e}")
            return []

    def format_response(self, response):
        articles = response.get('articles', [])
        return [{
            'title': a.get('title'),
            'url': a.get('url'),
            'source': 'GNews',
            'publishedAt': a.get('publishedAt'),
            'description': a.get('description', '')
        } for a in articles if a.get('url')]


# ==== NewsAPI ====
class NewsAPIOrgAdapter(NewsAPIAdapter):
    def __init__(self, api_key):
        self.api_key = api_key
        self.base_url = "https://newsapi.org/v2/everything"    


### NewsAPI has some methods of searching
## everything == find every url that adapted request
## headlines == find and response the news that is hot and have searched most
## source == return source of news (response is not a blog)





    def get_articles(self, query, **kwargs):
        params = {
            'q': query,
            'language': kwargs.get('lang', 'en'),
            'pageSize': kwargs.get('max', 10),
            'apiKey': self.api_key
        }
            # Thêm các tham số tùy chọn
        if kwargs.get('from_date'):
            params['from'] = kwargs['from_date']
        if kwargs.get('to_date'):
            params['to'] = kwargs['to_date']
        if kwargs.get('domains'):
            params['domains'] = kwargs['domains']  # vd: "cnn.com,bbc.co.uk"
        if kwargs.get('exclude_domains'):
            params['excludeDomains'] = kwargs['exclude_domains']  # vd: "foxnews.com"

        try:
            response = requests.get(self.base_url, params=params, timeout=10)
            response.raise_for_status()
            return self.format_response(response.json())
        except requests.RequestException as e:
            print(f"[NewsAPI ERROR]: {e}")
            return []

    def format_response(self, response):
        if response.get('status') != 'ok':
            return []
        articles = response.get('articles', [])
        return [{
            'title': a.get('title'),
            'url': a.get('url'),
            'source': a.get('source', {}).get('name', 'NewsAPI'),
            'publishedAt': a.get('publishedAt'),
            'description': a.get('description', '')
        } for a in articles if a.get('url')]


# ==== Nội dung từ newspaper ====
def get_content_from_urls(urls, content_type='text'):
    contents = []
    for url in urls:
        try:
            article = Article(url)
            article.download()
            article.parse()
            if content_type == 'html':
                contents.append(article.html)
            else:
                contents.append(article.text)
        except Exception as e:
            print(f"[Content ERROR]: {e}")
            contents.append("")
    return contents


# ==== Aggregator logic ====
class NewsAggregator:
    def __init__(self, max_workers=4):
        self.adapters = [GNewsAdapter(GNEWS_API), NewsAPIOrgAdapter(NEWS_API)]
        self.max_workers = max_workers

    def get_multiple_queries_articles(self, queries, **kwargs):
        all_articles = []

        def fetch_query(query):
            articles = []
            for adapter in self.adapters:
                articles += adapter.get_articles(query, **kwargs)
            return articles

        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = [executor.submit(fetch_query, q) for q in queries]
            for f in concurrent.futures.as_completed(futures):
                all_articles += f.result()

        return self.remove_duplicates(all_articles)

    def remove_duplicates(self, articles):
        seen = set()
        unique = []
        for article in articles:
            url = article.get("url")
            if url and url not in seen:
                seen.add(url)
                unique.append(article)
        return unique


# ==== FastAPI Endpoint ====
@app.post("/crawl-news")
def crawl_news(data: CrawlRequest):
    print(f"[INFO] Received query: {data.queries}")
    start_time = time.time()

    aggregator = NewsAggregator()
    articles = aggregator.get_multiple_queries_articles(
        data.queries,
        lang=data.lang,
        max=data.max,
        country=data.country,
        from_date=data.from_date,
        to_date=data.to_date,
        domain=data.domains
    )

    urls = [a['url'] for a in articles]
    contents = get_content_from_urls(urls, content_type=data.content_type)

    for i, article in enumerate(articles):
        article["content"] = contents[i]

    duration = time.time() - start_time

    return {
        "status": "success",
        "query_count": len(data.queries),
        "article_count": len(articles),
        "time_taken": f"{duration:.2f} sec",
        "articles": articles  # Trả về tối đa 10 bài, tránh quá nặng
    }
