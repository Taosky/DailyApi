# coding:utf-8
import os
from datetime import datetime, timedelta
import json
from subprocess import Popen
from config import ZHUANLAN_LIST
from flask import Flask, make_response, request
import PyRSS2Gen
from flask_cors import CORS
from utils import parse_ymd, get_json_data, get_article_type
from database import db_session
from models import Author, Day, Article, Comment, ArticleAuthor
from bs4 import BeautifulSoup

app = Flask(__name__)
CORS(app)


@app.teardown_appcontext
def shutdown_session(exception=None):
    db_session.remove()


# 添加作者到数据库
def add_author(authors):
    for author in authors:
        if Author.query.filter_by(name=author[0]).count() == 0:
            new_author = Author(name=author[0], avatar=author[1], bio=author[2])
            db_session.add(new_author)


# 添加评论到数据库
def add_comments(comments):
    for comment in comments:
        if Comment.query.filter_by(id=comment['id']).count() == 0:
            new_comment = Comment(id=comment['id'], author=comment['author'], content=comment['content'],
                                  likes=comment['likes'], time=comment['time'],
                                  reply_to=comment['reply_to']['id'] if 'reply_to' in comment and 'id' in comment[
                                      'reply_to'] else 0)
            db_session.add(new_comment)


# 获取文章html内的信息
def get_article_authors(html):
    soup = BeautifulSoup(html, 'html.parser')
    author_names = [node.get_text().strip('，。') for node in soup.find_all('span', class_='author')]
    author_avatars = [node['src'] for node in soup.find_all('img', class_='avatar')]
    # 作者信息
    authors = [(author_names[i], author_avatars[i], '') for i in range(len(author_names))]
    return authors


# 获取并添加评论到数据库
def get_article_comments(article_id):
    short_comments_api = 'https://news-at.zhihu.com/api/4/story/{}/short-comments'.format(article_id)
    long_comments_api = 'https://news-at.zhihu.com/api/4/story/{}/long-comments'.format(article_id)
    comments = get_json_data(short_comments_api)['comments'] + get_json_data(long_comments_api)['comments']

    authors = [(comment['author'], comment['avatar'], '') for comment in comments]

    return comments, authors


# 获取指定日期内容
def get_before(date_before):
    url = 'https://news-at.zhihu.com/api/4/news/before/{}'.format(date_before)
    json_data = get_json_data(url)
    date = json_data['date']
    day_query = Day.query.filter_by(date=date)
    if day_query.count() == 0:
        new_day = Day(date=date, data=json.dumps(json_data), update=datetime.now())
        db_session.add(new_day)
    else:
        day_query.update({'data': json.dumps(json_data), 'update': datetime.now()})

    all_comments = []
    all_authors = set()

    for story in json_data['stories']:
        if Article.query.filter_by(id=story['id']).count() == 0:
            api_url = 'https://news-at.zhihu.com/api/4/news/{}'.format(story['id'])
            article_data = get_json_data(api_url)
            article_authors = get_article_authors(article_data['body'])
            comments, comment_authors = get_article_comments(story['id'])
            article_type = get_article_type(article_data['title'])

            # 文章
            new_article = Article(id=story['id'], title=article_data['title'], date=date, url=article_data['share_url'],
                                  image=article_data['image'], type=article_type, data=json.dumps(article_data))
            db_session.add(new_article)

            # 评论
            for one_comment in comments:
                all_comments.append(one_comment)

            # 作者
            for one_author in (article_authors + comment_authors):
                all_authors.add(one_author)

            # 文章和作者
            for one_author in article_authors:
                new_article_author = ArticleAuthor(article_id=story['id'], author=one_author[0])
                db_session.add(new_article_author)

    add_author(all_authors)
    add_comments(all_comments)

    db_session.commit()


@app.route('/')
def index():
    return 'Forbidden', 403


@app.route('/v1/day/<date>')
def show_day(date):
    if Day.query.filter_by(date=date).count() == 0:
        before_date = (parse_ymd(date) + timedelta(days=1)).strftime('%Y%m%d')
        get_before(before_date)
    return Day.query.filter_by(date=date).first().data


@app.route('/v1/article/<a_id>')
def show_article(a_id):
    return Article.query.filter_by(id=a_id).first().data


@app.route('/v1/zhuanlan/<name>/rss')
def show_zhuanlan_rss(name):
    xml_file = 'zhuanlan/{}'.format(name)
    if not os.path.exists(xml_file):
        return '404', 404

    resp = make_response(open(xml_file, encoding='utf-8').read())
    resp.headers["Content-type"] = "application/xml;charset=UTF-8"
    return resp


# Github Webhook 用于前端更新
@app.route('/v1/webhook', methods=['POST'])
def webhook():
    if 'X-Hub-Signature' in request.headers and request.headers['X-Hub-Signature'].startswith('sha1='):
        p = Popen('/var/www/Daily/webhook.sh')
        return 'Ok'
    else:
        return 'Forbidden.', 403


def update_zhuanlan_rss():
    print('Start update zhuanlan rss...')
    for name in ZHUANLAN_LIST:
        api = 'https://www.zhihu.com/api/v4/columns/{}/articles'.format(name)
        data = get_json_data(api)['data']

        items = []
        for article in data:
            item = PyRSS2Gen.RSSItem(
                title=article['title'],
                link=article['url'],
                description=article['excerpt'],
                guid=article['url'],
                pubDate=datetime.fromtimestamp(int(article['created'])),
            )
            items.append(item)

        rss = PyRSS2Gen.RSS2(
            title="知乎专栏-{}".format(name),
            link="https://zhuanlan.zhihu.com/{}".format(name),
            description="知乎专栏-{}".format(name),

            lastBuildDate=datetime.now(),
            items=items)

        rss.write_xml(open('zhuanlan/{}'.format(name), 'w', encoding='utf-8'))

        print('Finished zhuanlan update.')


def update_daily():
    print('Start update daily...')
    # 23点后更新今天的内容
    if datetime.now().hour > 23:
        date_before = (datetime.now() + timedelta(days=1)).strftime('%Y%m%d')
    # 其他时间更新昨天的内容
    else:
        date_before = datetime.now().strftime('%Y%m%d')
    get_before(date_before)
    print('Finished daily update.')


def update():
    update_daily()
    update_zhuanlan_rss()
    print('\nAll updated')


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5661)
