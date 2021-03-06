# Copyright 2014-2015 Ian Denhardt <ian@zenhack.net>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>
from datetime import datetime
import time
import logging

from flask.ext.sqlalchemy import SQLAlchemy
from flask.ext.login import UserMixin
from passlib.hash import sha512_crypt
import feedparser
import jinja2

import ironblogger.date
from ironblogger.date import duedate, ROUND_LEN, divide_timedelta

DEBT_PER_POST = 500
LATE_PENALTY = 100

feedparser.USER_AGENT = \
        'IronBlogger/git ' + \
        '+https://github.com/zenhack/iron-blogger2 ' + \
        feedparser.USER_AGENT

db = SQLAlchemy()


class MalformedPostError(Exception):
    """Raised when parsing a post fails."""


class User(db.Model, UserMixin):
    """A user of Iron Blogger.

    In practice, this will almost always correspond to a blogger, but
    theoretically an admin might not themselves be a participant, so the
    concepts are kept separate.
    """
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String, nullable=False, unique=True)
    is_admin = db.Column(db.Boolean, nullable=False)

    # Hahsed & salted password. null means the account hasn't been activated.
    hashed_password = db.Column(db.String)

    blogger_id = db.Column(db.Integer, db.ForeignKey('blogger.id'), unique=True)
    blogger = db.relationship('Blogger', backref=db.backref('user', uselist=False))

    def verify_password(self, password):
        if self.hashed_password is None:
            return False
        else:
            return sha512_crypt.verify(password, self.hashed_password)

    def set_password(self, password):
        self.hashed_password = sha512_crypt.encrypt(password)

    def get_id(self):
        """Slightly non-intuitively returns self.name.

        This is here for the benefit of Flask-Login.
        """
        return self.name


class Blogger(db.Model):
    """An Iron Blogger participant."""
    id         = db.Column(db.Integer,  primary_key=True)
    name       = db.Column(db.String,   nullable=False, unique=True)
    start_date = db.Column(db.DateTime, nullable=False)

    def __repr__(self):
        return self.name


class Blog(db.Model):
    """A blog. bloggers may have more than one of these."""
    id         = db.Column(db.Integer, primary_key=True)
    blogger_id = db.Column(db.Integer, db.ForeignKey('blogger.id'), nullable=False)
    title      = db.Column(db.String, nullable=False)
    page_url   = db.Column(db.String, nullable=False)  # Human readable webpage
    feed_url   = db.Column(db.String, nullable=False)  # Atom/RSS feed
    # Metadata for caching:
    etag       = db.Column(db.String)  # see: https://pythonhosted.org/feedparser/http-etag.html
    modified   = db.Column(db.String)  # We don't bother parsing this; it's only for the server's
                                       # Benefit.

    blogger = db.relationship('Blogger', backref=db.backref('blogs'))

    def sync_posts(self):
        logging.info('Syncing posts for blog %r by %r',
                     self.title,
                     self.blogger.name)
        last_post = db.session.query(Post)\
            .filter_by(blog=self)\
            .order_by(Post.timestamp.desc()).first()
        feed = feedparser.parse(self.feed_url,
                                etag=self.etag,
                                modified=self.modified)
        if hasattr(feed, 'status') and feed.status == 304:
            logging.info('Feed for blog %r (by %r) was not modified.',
                         self.title,
                         self.blogger.name)

        feed_posts = map(Post.from_feed_entry, feed.entries)

        # The loop below assumes our feed entries are sorted by date, newest
        # first. This ensures just that:
        feed_posts = sorted([(post.timestamp, post) for post in feed_posts])
        feed_posts.reverse()
        feed_posts = [post for (_date, post) in feed_posts]

        for post in feed_posts:
            # Check if the post is already in the db:
            if last_post is not None:
                if post.timestamp < last_post.timestamp:
                    # We can stop storing posts when we get to one that's older
                    # than one we already have. Note that we can't do less than
                    # or equal here, since someone might post more than one
                    # post in a day.
                    break
                if post.timestamp == last_post.timestamp and post.title == last_post.title:
                    # If a post has the same date as one already in the db (on
                    # the same blog), we use the title as an identifier.
                    break

            post.blog = self
            db.session.add(post)
            logging.info('Added new post %r', post.page_url)
        self._update_caching_info(feed)
        db.session.commit()


    def _update_caching_info(self, feed):
        if hasattr(feed, 'etag'):
            self.etag = feed.etag
        if hasattr(feed, 'modified'):
            self.modified = feed.modified


class Party(db.Model):
    id    = db.Column(db.Integer, primary_key=True)
    date  = db.Column(db.Date,    nullable=False)
    spent = db.Column(db.Integer, nullable=False)


class Payment(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    blogger_id = db.Column(db.Integer, db.ForeignKey('blogger.id'), nullable=False)
    amount     = db.Column(db.Integer, nullable=False)  # monetary amount, in units
                                                        # of $0.01 USD.
                                                        # Internationalization
                                                        # is still TODO.

    blogger = db.relationship('Blogger', backref=db.backref('payments'))


class Post(db.Model):
    """A blog post."""
    id         = db.Column(db.Integer,  primary_key=True)
    blog_id    = db.Column(db.Integer,  db.ForeignKey('blog.id'), nullable=False)
    timestamp  = db.Column(db.DateTime, nullable=False)
    counts_for = db.Column(db.DateTime)
    title      = db.Column(db.String,   nullable=False)
    # The *sanitized* description/summary field from the feed entry. This will
    # be copied directly to the generated html, so sanitization is critical:
    summary    = db.Column(db.Text,     nullable=False)
    page_url   = db.Column(db.String,   nullable=False)

    blog  = db.relationship('Blog',  backref=db.backref('posts'))

    @staticmethod
    def _get_pub_date(feed_entry):
        """Return a datetime.datetime object for the post's publication date.

        ``feed_entry`` should be a post object as returned by
        ``feedparser.parse``.

        If the post does not have a publication date, raise a
        ``MalformedPostError``.
        """
        for key in 'published', 'created', 'updated':
            key += '_parsed'
            if key in feed_entry and feed_entry[key] is not None:
                return datetime.fromtimestamp(time.mktime(feed_entry[key]))
        raise MalformedPostError("No valid publication date in post: %r" %
                                 feed_entry)

    @staticmethod
    def from_feed_entry(entry):
        """Read and construct Post object from ``entry``.

        ``entry`` should be a post object as returned by ``feedparser.parse``.

        If the post is invalid, raise a ``MalformedPostError`.

        This leaves the `blog` field emtpy; this must be filled in before the
        post is added to the database.
        """
        for field in 'title', 'summary', 'link':
            if field not in entry:
                raise MalformedPostError("Post has no %s: %r" % (field, entry))
        post = Post()
        post.timestamp = Post._get_pub_date(entry)
        post.title = entry['title']
        post.summary = entry['summary']

        # The summary detail attribute lets us find the mime type of the
        # summary. feedparser doesn't escape it if it's text/plain, so we need
        # to do it ourselves. Unfortunately, there's a bug (likely #412) in
        # feedparser, and sometimes this attribute is unavailable. If it's
        # there, great, use it. Otherwise, we'll just assume it's html, and
        # sanitize it ourselves.
        if hasattr(entry, 'summary_detail'):
            mimetype = entry.summary_detail.type
        else:
            mimetype = 'application/xhtml'
            # Sanitize the html; who knows what feedparser did or didn't do.
            # XXX: _sanitizeHTML is a private function to the feedparser
            # library! unfortunately, we don't have many better options. This
            # statement is the reason the version number for the feedparser
            # dependency is fixed at 5.1.3; any alternate version will need to
            # be vetted carefully, as by doing this we lose any api stability
            # guarantees.
            post.summary = unicode(feedparser._sanitizeHTML(
                # _sanitizeHTML expects an encoding, so rather than do more
                # guesswork than we alredy have...
                post.summary.encode('utf-8'),
                'utf-8',
                # _sanitizeHTML is only ever called within the library with
                # this value:
                u'text/html',
            ), 'utf-8')

        if mimetype == 'text/plain':
            # feedparser doesn't sanitize the summary if it's plain text, so we
            # need to do it manually. We're using jijna2's autoscape feature
            # for this, which feels like a bit of a hack to me (Ian), but it
            # works -- there's probably a cleaner way to do this.
            tmpl = jinja2.Template('{{ text }}', autoescape=True)
            post.summary = tmpl.render(text=post.summary)
        post.page_url = entry['link']

        return post

    def rssdate(self):
        return ironblogger.date.rssdate(self.timestamp)

    def assign_round(self):
        # Get all of the dates that this post could count for, but which are
        # "taken" by other posts.
        oldest_valid_duedate = duedate(self.timestamp) - ROUND_LEN * (DEBT_PER_POST / LATE_PENALTY)
        oldest_valid_duedate = max(oldest_valid_duedate, duedate(self.blog.blogger.start_date))
        dates = db.session.query(Post.counts_for)\
            .filter(Post.counts_for != None,
                    Post.counts_for <= duedate(self.timestamp),
                    Post.counts_for >= oldest_valid_duedate,
                    Post.blog_id == Blog.id,
                    Blog.blogger_id == self.blog.blogger.id)\
            .order_by(Post.counts_for.desc())\
            .all()
        dates = set([date[0] for date in dates])

        # Assign the most recent round this post can count for.
        round = duedate(self.timestamp)
        while round >= oldest_valid_duedate:
            if round not in dates:
                self.counts_for = round
                break
            round -= ROUND_LEN

    def rounds_late(self):
        """How late is this post (in weeks)?

        If the post counts for some round, return the number of weeks this post
        is late (rounded up). If the post does not count for any round (because
        all rounds this post *could* count for are taken), return None.
        """
        if self.counts_for is None:
            return None

        return divide_timedelta(duedate(self.timestamp) - self.counts_for,
                                ROUND_LEN)
