"""add uniqueness constraint on citation combinations

Revision ID: e218cbe0b4bb
Revises: f532a873941c
Create Date: 2021-12-20 15:47:41.101340

"""
from alembic import op, context
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e218cbe0b4bb'
down_revision = 'f532a873941c'
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_unique_constraint(None, 'citation', ['citing_opinion_id', 'cited_opinion_id'])
    # ### end Alembic commands ###
    if context.get_x_argument(as_dictionary=True).get('data', None):
        data_upgrade()


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_constraint(None, 'citation', type_='unique')
    # ### end Alembic commands ###
    if context.get_x_argument(as_dictionary=True).get('data', None):
        data_downgrade()


def data_upgrade():
    pass


def data_downgrade():
    pass
