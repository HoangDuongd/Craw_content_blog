from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime
import time
from newspaper import Article
import requests
import concurrent.futures
import asyncio
import logging

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

NEWS_API = '898c89c237e243599b404ebd48f2b6a3'
GNEWS_API = '3dd8a59498ec187ddeae7e78d573d108'

class CrawlRequest(BaseModel):
    queries: List[str]
    lang: Optional[str] = "en"
    max: Optional[int] = 10
    from_date: Optional[str] = None
    to_date: Optional[str] = None
    content_type: Optional[str] = "text"
    country: Optional[List[str]] = ["us"]
    domains: Optional[List[str]] = None
    exclude_domains: Optional[List[str]] = None

# Add health check endpoints
@app.get("/")
async def root():
    return {"message": "News Crawler API is running", "status": "healthy"}

@app.get("/health")
async def health_check():
    return {"status": "healthy", "message": "Server is running"}

from abc import ABC, abstractmethod

class NewsAPIAdapter(ABC):
    @abstractmethod
    def get_articles(self, query, **kwargs):
        pass

    @abstractmethod
    def format_response(self, response):
        pass

class GNewsAdapter(NewsAPIAdapter):
    def __init__(self, api_key):
        self.api_key = api_key
        self.base_url = "https://gnews.io/api/v4/search"

    def get_articles(self, query, **kwargs):
        country = kwargs.get('country', ['us'])
        if isinstance(country, list):
            country = country[0]

        params = {
            'q': query,
            'token': self.api_key,
            'lang': kwargs.get('lang', 'en'),
            'country': country,
            'max': kwargs.get('max', 10)
        }
        try:
            # Tăng timeout và thêm retry logic
            response = requests.get(self.base_url, params=params, timeout=30)
            response.raise_for_status()
            return self.format_response(response.json())
        except requests.RequestException as e:
            logger.error(f"[GNews ERROR]: {e}")
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

class NewsAPIOrgAdapter(NewsAPIAdapter):
    def __init__(self, api_key):
        self.api_key = api_key
        self.base_url = "https://newsapi.org/v2/everything"

    def get_articles(self, query, **kwargs):
        params = {
            'q': query,
            'language': kwargs.get('lang', 'en'),
            'pageSize': kwargs.get('max', 10),
            'apiKey': self.api_key
        }

        if kwargs.get('from_date'):
            params['from'] = kwargs['from_date']
        if kwargs.get('to_date'):
            params['to'] = kwargs['to_date']
        if kwargs.get('domains'):
            params['domains'] = ",".join(kwargs['domains'])
        if kwargs.get('exclude_domains'):
            params['excludeDomains'] = ",".join(kwargs['exclude_domains'])

        try:
            # Tăng timeout
            response = requests.get(self.base_url, params=params, timeout=30)
            response.raise_for_status()
            return self.format_response(response.json())
        except requests.RequestException as e:
            logger.error(f"[NewsAPI ERROR]: {e}")
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
            logger.error(f"[Content ERROR for {url}]: {e}")
            contents.append("")
    return contents

class NewsAggregator:
    def __init__(self, max_workers=2):  # Giảm max_workers
        self.adapters = [GNewsAdapter(GNEWS_API), NewsAPIOrgAdapter(NEWS_API)]
        self.max_workers = max_workers

    def get_multiple_queries_articles(self, queries, **kwargs):
        all_articles = []
        exclude_domains = kwargs.get("exclude_domains") or []

        def fetch_query(query):
            articles = []
            for adapter in self.adapters:
                try:
                    articles += adapter.get_articles(query, **kwargs)
                except Exception as e:
                    logger.error(f"Error fetching query '{query}': {e}")
            return articles

        # Giảm số workers để tránh overload
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = [executor.submit(fetch_query, q) for q in queries]
            for f in concurrent.futures.as_completed(futures):
                try:
                    all_articles += f.result()
                except Exception as e:
                    logger.error(f"Error processing future: {e}")

        filtered_articles = self.remove_duplicates(all_articles)

        if exclude_domains:
            filtered_articles = self.filter_excluded_domains(filtered_articles, exclude_domains)

        return filtered_articles

    def remove_duplicates(self, articles):
        seen = set()
        unique = []
        for article in articles:
            url = article.get("url")
            if url and url not in seen:
                seen.add(url)
                unique.append(article)
        return unique

    def filter_excluded_domains(self, articles, exclude_domains):
        def is_excluded(url):
            for domain in exclude_domains:
                if domain in url:
                    return True
            return False

        return [article for article in articles if not is_excluded(article.get("url", ""))]

# Sử dụng async để tránh blocking
@app.post("/crawl-news")
async def crawl_news(data: CrawlRequest):
    try:
        logger.info(f"[INFO] Received query: {data.queries}")
        start_time = time.time()

        # Validate input
        if not data.queries or all(not q.strip() for q in data.queries):
            raise HTTPException(status_code=400, detail="At least one non-empty query is required")

        aggregator = NewsAggregator()

        # Làm sạch dữ liệu nếu có giá trị rỗng
        domains = [d for d in data.domains if d.strip()] if data.domains else None
        exclude_domains = [d for d in data.exclude_domains if d.strip()] if data.exclude_domains else None
        country = data.country if data.country else ['us']

        # Chạy trong thread pool để tránh block main thread
        loop = asyncio.get_event_loop()
        articles = await loop.run_in_executor(
            None,
            lambda: aggregator.get_multiple_queries_articles(
                data.queries,
                lang=data.lang,
                max=data.max,
                country=country,
                from_date=data.from_date,
                to_date=data.to_date,
                domains=domains,
                exclude_domains=exclude_domains
            )
        )

        if not articles:
            logger.warning("No articles found")
            return []

        urls = [a['url'] for a in articles]
        
        # Giới hạn số lượng URLs để tránh timeout
        max_urls = min(len(urls), 20)  # Giới hạn 20 URLs
        urls = urls[:max_urls]
        articles = articles[:max_urls]

        contents = await loop.run_in_executor(
            None,
            lambda: get_content_from_urls(urls, content_type=data.content_type)
        )

        for i, article in enumerate(articles):
            article["content"] = contents[i] if i < len(contents) else ""

        duration = time.time() - start_time
        logger.info(f"Processing completed in {duration:.2f} seconds")

        # Tạo danh sách bài viết chỉ gồm các trường cần thiết
        table_articles = [
            {
                "title": a.get("title", ""),
                "source": a.get("source", ""),
                "content": a.get("content", "")
            }
            for a in articles
        ]

        return table_articles

    except Exception as e:
        logger.error(f"Error in crawl_news: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

# Thêm exception handler
@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    logger.error(f"Global exception: {str(exc)}")
    return {"error": "Internal server error", "detail": str(exc)}

# Main runner
if __name__ == "__main__":
    import uvicorn
    import os
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
